from contextlib import nullcontext
from datetime import datetime
from time import sleep, time

import humanize
from celery.result import AsyncResult
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import (Progress, SpinnerColumn, TextColumn,
						   TimeElapsedColumn)

from secator.definitions import DEBUG
from secator.output_types import OUTPUT_TYPES, OutputType
from secator.report import Report
from secator.rich import console, console_stdout
from secator.runners._helpers import (get_task_ids, get_task_info,
									process_extractor)
from secator.utils import import_dynamic, merge_opts, pluralize
from dotmap import DotMap
import json
import sys
import logging
import uuid


logger = logging.getLogger(__name__)

HOOKS = [
	'on_init',
	'on_start',
	'on_end',
	'on_item_pre_convert',
	'on_item',
	'on_line',
	'on_iter',
	'on_error',
	'on_init'
]

VALIDATORS = [
	'input',
	'item'
]


class Runner:
	"""Runner class.

	Args:
		config (secator.config.ConfigLoader): Loaded config.
		targets (list): List of targets to run task on.
		results (list): List of existing results to re-use.
		workspace_name (str): Workspace name.
		run_opts (dict): Run options.

	Yields:
		dict: Result (when running in sync mode with `run`).

	Returns:
		list: List of results (when running in async mode with `run_async`).
	"""

	# Input field (mostly for tests and CLI)
	input_type = None

	# Output types
	output_types = []

	# Dict return
	output_return_type = dict  # TODO: deprecate this

	# Default exporters
	default_exporters = []

	# Run hooks
	enable_hooks = True

	def __init__(self, config, targets, results=[], run_opts={}, hooks={}, context={}):
		self.config = config
		self.name = run_opts.get('name', config.name)
		self.description = run_opts.get('description', config.description)
		if not isinstance(targets, list):
			targets = [targets]
		self.targets = targets
		self.results = results
		self.results_count = 0
		self.workspace_name = context.get('workspace_name', 'default')
		self.run_opts = run_opts.copy()
		self.sync = run_opts.get('sync', True)
		self.exporters = self.resolve_exporters()
		self.done = False
		self.start_time = datetime.fromtimestamp(time())
		self.end_time = None
		self._hooks = hooks
		self.errors = []
		self.output = ''
		self.status = 'RUNNING'
		self.progress = 0
		self.context = context
		self.delay = run_opts.get('delay', False)
		self.uuids = []
		self.result = None

		# Process input
		self.input = targets
		if isinstance(self.input, list) and len(self.input) == 1:
			self.input = self.input[0]

		# Yield dicts if CLI supports JSON
		if self.output_return_type is dict or (self.json_flag is not None):
			self.output_return_type = dict

		# Print options
		self.print_start = self.run_opts.pop('print_start', False)
		self.print_results = self.run_opts.pop('print_results', False)
		self.print_item = self.run_opts.pop('print_item', False)
		self.print_line = self.run_opts.pop('print_line', False)
		self.print_item_count = self.run_opts.pop('print_item_count', False)
		self.print_cmd = self.run_opts.pop('print_cmd', False)
		self.print_input_file = self.run_opts.pop('print_input_file', False)
		self.print_progress = self.run_opts.pop('print_progress', False)
		self.print_cmd_prefix = self.run_opts.pop('print_cmd_prefix', False)
		self.print_remote_status = self.run_opts.pop('print_remote_status', False)
		self.print_run_summary = self.run_opts.pop('print_run_summary', False)
		self.print_json = self.run_opts.get('json', True)
		self.print_raw = self.run_opts.get('raw', False)
		self.print_orig = self.run_opts.get('orig', False)

		# Output options
		self.output_fmt = self.run_opts.get('format', False)
		self.output_quiet = self.run_opts.get('quiet', False)
		self.output_json = self.output_return_type == dict

		# Hooks
		self.hooks = {name: [] for name in HOOKS}
		for key in self.hooks:
			instance_func = getattr(self, key, None)
			if instance_func:
				self.hooks[key].append(instance_func)
			self.hooks[key].extend(hooks.get(self.__class__, {}).get(key, []))

		# Validators
		self.validators = {name: [] for name in VALIDATORS}
		for key in self.validators:
			instance_func = getattr(self, f'validate_{key}', None)
			if instance_func:
				self.validators[key].append(instance_func)
			self.validators[key].extend(self.validators.get(self.__class__, {}).get(key, []))

		# Chunks
		self.chunk = self.run_opts.get('chunk', None)
		self.chunk_count = self.run_opts.get('chunk_count', None)
		self._set_print_prefix()

		# Abort if inputs are invalid
		self.input_valid = True
		if not self.run_validators('input', self.input):
			self.input_valid = False

		self.run_hooks('on_init')

	@property
	def elapsed(self):
		if self.done:
			return self.end_time - self.start_time
		return datetime.fromtimestamp(time()) - self.start_time

	@property
	def elapsed_human(self):
		return humanize.naturaldelta(self.elapsed)

	def run(self):
		return list(self.__iter__())

	def __iter__(self):
		if self.print_start:
			self.log_start()

		if not self.input_valid:
			return

		try:
			for item in self.yielder():

				if isinstance(item, (OutputType, DotMap, dict)):

					# Handle direct yield of item
					item = self._process_item(item)
					if not item:
						continue

					# Discard item if needed
					if item._uuid in self.uuids:
						continue

					# Treat progress item
					if item._type == 'progress':
						if self.print_progress:
							self._print(str(item), out=sys.stderr, color='dim cyan')
						continue

					# Add item to results
					self.results.append(item)
					self.results_count += 1
					self.uuids.append(item._uuid)
					yield item

					# Print JSON or raw item
					if self.print_item and item._type != 'target':
						if self.print_json:
							self._print(item, out=sys.stdout)
						elif self.print_raw:
							self._print(str(item), out=sys.stdout)
						else:
							self._print(self.get_repr(item), out=sys.stdout)

				elif isinstance(item, str):
					if self.print_line:
						self._print(item, out=sys.stderr)
					if not self.output_json:
						self.results.append(item)
						yield item

				if item:
					self.output += str(item) + '\n'

				self.run_hooks('on_iter')

		except KeyboardInterrupt:
			self._print('Process was killed manually (CTRL+C / CTRL+X).', color='bold red', rich=True)
			if self.result:
				self._print('Revoking remote Celery tasks ...', color='bold red', rich=True)
				self.stop_live_tasks(self.result)

		# Filter results and log info
		self.results = self.filter_results()
		self.log_results()
		self.run_hooks('on_end')

	def yielder(self):
		raise NotImplementedError()

	def toDict(self):
		return {
			'config': self.config.toDict(),
			'opts': self.config.supported_opts,
			'name': self.name,
			'targets': self.targets,
			'run_opts': self.run_opts,
			'chunk': self.chunk,
			'chunk_count': self.chunk_count,
			'results_count': self.results_count,
			'sync': self.sync,
			'done': self.done,
			'output': self.output,
			'status': self.status,
			'progress': self.progress,
			'start_time': self.start_time,
			'end_time': self.end_time,
			'elapsed_human': self.elapsed_human,
			'errors': self.errors,
			'context': self.context
		}

	def run_hooks(self, hook_type, *args):
		# logger.debug(f'Running hooks of type {hook_type}')
		result = args[0] if len(args) > 0 else None
		if not self.enable_hooks:
			return result
		for hook in self.hooks[hook_type]:
			# logger.debug(hook)
			result = hook(self, *args)
		return result

	def run_validators(self, validator_type, *args):
		# logger.debug(f'Running validators of type {validator_type}')
		for validator in self.validators[validator_type]:
			# logger.debug(validator)
			if not validator(self, *args):
				if validator_type == 'input':
					self._print(f'{validator.__doc__}', color='bold red')
				return False
		return True

	def resolve_exporters(self):
		"""Resolve exporters from output options."""
		output = self.run_opts.get('output', '')
		if output == '':
			return self.default_exporters
		elif output is False:
			return []
		exporters = [
			import_dynamic(f'secator.exporters.{o.capitalize()}Exporter', 'Exporter')
			for o in output.split(',')
			if o
		]
		return [e for e in exporters if e]

	def log_start(self):
		"""Log runner start."""
		remote_str = 'starting' if self.sync else 'sent to Celery worker'
		runner_name = self.__class__.__name__
		self.log_header()
		self._print(
			f':tada: {runner_name} [bold magenta]{self.config.name}[/] {remote_str}...', rich=True, with_timestamp=True)
		sleep(1)  # needed to show timestamp
		if not self.sync and self.__class__.__name__ != 'Scan':
			self._print('🏆 [bold gold3]Live results:[/]', rich=True)

	def log_header(self):
		"""Log runner header."""
		runner_name = self.__class__.__name__

		# Description
		panel_str = f':scroll: [bold gold3]Description:[/] {self.config.description}'

		# Workspace
		if self.workspace_name:
			panel_str += f'\n:construction_worker: [bold gold3]Workspace:[/] {self.workspace_name}'

		# Targets
		if self.targets:
			panel_str += '\n:pear: [bold gold3]Targets:[/]'
			for target in self.targets:
				panel_str += f'\n   • {target}'

		# Options
		DISPLAY_OPTS_EXCLUDE = [
			'sync', 'worker', 'debug', 'output', 'json', 'orig', 'raw', 'format', 'quiet'
		]
		items = [
			f'[italic]{k}[/]: {v}'
			for k, v in self.run_opts.items()
			if k not in DISPLAY_OPTS_EXCLUDE
			and v is not None
		]
		if items:
			panel_str += '\n:pushpin: [bold gold3]Options:[/]'
			for item in items:
				panel_str += f'\n   • {item}'

		if self.exporters:
			panel_str += '\n:email:  [bold gold3]Exporters:[/]'
			for exporter in self.exporters:
				exporter_name = exporter.__name__.replace('Exporter', '').lower()
				panel_str += f'\n   • {exporter_name}'

		panel = Panel(
			panel_str,
			title=f'[bold gold3]{runner_name}[/] [bold magenta]{self.config.name}[/]',
			border_style='bold gold3',
			expand=False,
			highlight=True
		)
		self._print(panel, rich=True)

	def log_results(self):
		"""Log results.

		Args:
			results (list): List of results.
			output_types (list): List of result types to add to report.
		"""
		self.done = True
		self.progress = 100
		self.results_count = len(self.results)
		self.status = 'SUCCESS' if not self.errors else 'FAILED'
		self.end_time = datetime.fromtimestamp(time())

		# Log execution results
		status = 'succeeded' if not self.errors else '[bold red]failed[/]'
		if self.print_run_summary:
			self._print('\n')
			self._print(
				f':tada: [bold green]{self.__class__.__name__.capitalize()}[/] [bold magenta]{self.config.name}[/] '
				f'[bold green]{status} in[/] [bold gold3]{self.elapsed_human}[/].', rich=True, with_timestamp=True)

		# Log runner errors
		for error in self.errors:
			self._print(error, color='bold red')

		# Build and send report
		if self.results:
			report = Report(self, exporters=self.exporters)
			report.build()
			report.send()
			self.report = report

		# Log results count
		if self.print_item_count and self.print_json and not self.print_raw and not self.print_orig:
			count_map = self._get_results_count()
			if all(count == 0 for count in count_map.values()):
				self._print(':adhesive_bandage: Found 0 results.', color='bold red', rich=True)
			else:
				results_str = ':pill: Found ' + ' and '.join([
					f'{count} {pluralize(name) if count > 1 or count == 0 else name}'
					for name, count in count_map.items()
				]) + '.'
				self._print(results_str, color='bold green', rich=True)

	@staticmethod
	def get_live_results(result):
		"""Poll Celery subtasks results in real-time. Fetch task metadata and partial results from each task that runs.

		Args:
			result (celery.result.AsyncResult): Result object.

		Yields:
			dict: Subtasks state and results.
		"""
		res = AsyncResult(result.id)
		while True:
			task_ids = []
			get_task_ids(result, ids=task_ids)
			for task_id in task_ids:
				info = get_task_info(task_id)
				if not info:
					continue
				yield info

			# Break out of while loop
			if res.ready():
				break

			# Sleep between updates
			sleep(1)

	def stop_live_tasks(self, result):
		"""Stop live tasks running in Celery worker.

		Args:
			result (AsyncResult | GroupResult): Celery result.
		"""
		task_ids = []
		get_task_ids(result, ids=task_ids)
		for task_id in task_ids:
			from secator.celery import revoke_task
			revoke_task(task_id)

	def process_live_tasks(self, result, description=True, results_only=True, print_remote_status=True):
		"""Rich progress indicator showing live tasks statuses.

		Args:
			result (AsyncResult | GroupResult): Celery result.
			results_only (bool): Yield only results, no task state.

		Yields:
			dict: Subtasks state and results.
		"""
		config_name = self.config.name
		runner_name = self.__class__.__name__.capitalize()

		# Display live results if print_remote_status is set
		if print_remote_status:
			class PanelProgress(Progress):
				def get_renderables(self):
					yield Padding(Panel(
						self.make_tasks_table(self.tasks),
						title=f'[bold gold3]{runner_name}[/] [bold magenta]{config_name}[/] results',
						border_style='bold gold3',
						expand=False,
						highlight=True), pad=(2, 0, 0, 0))

			tasks_progress = PanelProgress(
				SpinnerColumn('dots'),
				TextColumn('{task.fields[descr]}  ') if description else '',
				TextColumn('[bold cyan]{task.fields[name]}[/]'),
				TextColumn('[dim gold3]{task.fields[chunk_info]}[/]'),
				TextColumn('{task.fields[state]:<20}'),
				TimeElapsedColumn(),
				TextColumn('{task.fields[count]}'),
				# TextColumn('\[[bold magenta]{task.fields[id]:<30}[/]]'),  # noqa: W605
				refresh_per_second=1,
				transient=True,
				# console=console,
				# redirect_stderr=True,
				# redirect_stdout=False
			)
			state_colors = {
				'RUNNING': 'bold yellow',
				'SUCCESS': 'bold green',
				'FAILURE': 'bold red',
				'REVOKED': 'bold magenta'
			}
		else:
			tasks_progress = nullcontext()

		with tasks_progress as progress:

			# Make progress tasks
			tasks_progress = {}

			# Get live results and print progress
			for info in Runner.get_live_results(result):

				# Re-yield so that we can consume it externally
				if results_only:
					yield from info['results']
				else:
					yield info

				if not print_remote_status:
					continue

 				# Ignore partials in output unless DEBUG > 1
				# TODO: weird to change behavior based on debug flag, could cause issues
				if info['chunk'] and not DEBUG > 1:
					continue

				# Handle error if any
				# TODO: error handling should be moved to process_live_tasks
				state = info['state']
				if info['error']:
					state = 'FAILURE'
					error = 'Error in task {name} {chunk_info}:\n{error}'.format(**info)
					if error not in self.errors:
						self.errors.append(error)

				task_id = info['id']
				state_str = f'[{state_colors[state]}]{state}[/]'
				info['state'] = state_str

				if task_id not in tasks_progress:
					id = progress.add_task('', **info)
					tasks_progress[task_id] = id
				else:
					progress_id = tasks_progress[task_id]
					if state in ['SUCCESS', 'FAILURE']:
						progress.update(progress_id, advance=100, **info)

			# Update all tasks to 100 %
			for progress_id in tasks_progress.values():
				progress.update(progress_id, advance=100)

	def filter_results(self):
		"""Filter runner results using extractors defined in config."""
		extractors = self.config.results
		results = []
		if extractors:
			# Keep results based on extractors
			opts = merge_opts(self.config.options, self.run_opts)
			for extractor in extractors:
				tmp = process_extractor(self.results, extractor, ctx=opts)
				results.extend(tmp)

			# Keep the field types in results not specified in the extractors.
			extract_fields = [e['type'] for e in extractors]
			keep_fields = [
				_type for _type in OUTPUT_TYPES
				if _type not in extract_fields
			]
			results.extend([
				item for item in self.results
				if item._type in keep_fields
			])
		else:
			results = self.results
		return results

	def _convert_item_schema(self, item):
		"""Convert dict item to a new structure using the class output schema.

		Args:
			item (dict): Item.

		Returns:
			dict: Item with new schema.
		"""
		# Load item using available output types and get the first matching
		# output type based on the schema
		new_item = None
		output_types = getattr(self, 'output_types', [])
		for klass in output_types:
			output_map = getattr(self, 'output_map', {})
			output_map = output_map.get(klass, {})
			try:
				new_item = klass.load(item, output_map)
				break  # found an item that fits
			except (TypeError, KeyError) as e:  # can't load using class
				if DEBUG > 2:
					logger.debug(f'Failed loading item with {klass}: {str(e)}. Continuing')
					raise e
				continue

		# No output type was found, so make no conversion
		if not new_item:
			new_item = DotMap(item)
			new_item._type = 'unknown'

		return new_item

	def _print(self, data, color=None, out=sys.stderr, rich=False, with_timestamp=False):
		"""Print function.

		Args:
			data (str or dict): Input data.
			color (str, Optional): Termcolor color.
			out (str, Optional): Output pipe (sys.stderr, sys.stdout, ...)
			rich (bool, Optional): Force rich output.
		"""
		# Choose rich console
		_console = console_stdout if out == sys.stdout else console

		# Print a JSON item
		if isinstance(data, (OutputType, DotMap, dict)):
			if getattr(data, 'toDict', None):
				data = data.toDict()
			data = json.dumps(data)
			data = f'{self.prefix:>15} {data}' if self.prefix and not self.print_item else data
			print(data, file=out)

		# Print a line with timestamp
		elif with_timestamp:
			_console.log(data, highlight=False, style=color)

		# Print a line
		else:
			_console.print(data, highlight=False, style=color) if self.sync or rich else print(data, file=out)

	def _set_print_prefix(self):
		self.prefix = ''
		if self.print_cmd_prefix:
			self.prefix = f'[bold gold3]({self.config.name})[/]'
		if self.chunk and self.chunk_count:
			self.prefix += f' [{self.chunk}/{self.chunk_count}]'

	def _get_results_count(self):
		count_map = {}
		for output_type in self.output_types:
			if output_type.__name__ == 'Progress':
				continue
			name = output_type.get_name()
			count = len([r for r in self.results if r._type == name])
			count_map[name] = count
		return count_map

	def _process_item(self, item: dict):
		# Run item validators
		if not self.run_validators('item', item):
			return None

		# Run item hooks
		item = self.run_hooks('on_item_pre_convert', item)
		if not item:
			return None

		# Convert output dict to another schema
		if isinstance(item, dict) and not self.print_orig:
			item = self._convert_item_schema(item)
		elif isinstance(item, OutputType):
			pass
		else:
			item = DotMap(item)

		# Add context to item
		item._source = self.config.name
		item._context = self.context
		if not item._uuid:
			item._uuid = str(uuid.uuid4())
		if item._type == 'progress':
			self.progress = item.percent

		# Run item convert hooks
		if not self.print_orig:
			item = self.run_hooks('on_item', item)

		# Return item
		return item

	def get_repr(self, item=None):
		if not item:
			return [
				self.get_repr(item)
				for item in self.results
			]
		if self.output_fmt:
			item = self.output_fmt.format(**item.toDict())
		elif isinstance(item, OutputType):
			item = repr(item)
		return item
