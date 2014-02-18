# Copyright 2013: Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import json
import jsonschema

from rally.benchmark import base
from rally.benchmark import runner
from rally import consts
from rally import exceptions
from rally.objects import endpoint
from rally.openstack.common.gettextutils import _
from rally.openstack.common import log as logging
from rally import osclients
from rally import utils as rutils


LOG = logging.getLogger(__name__)


CONFIG_SCHEMA = {
    "type": "object",
    "$schema": "http://json-schema.org/draft-03/schema",
    "patternProperties": {
        ".*": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "args": {"type": "object"},
                    "init": {"type": "object"},
                    "execution": {"enum": ["continuous", "periodic"]},
                    "config": {
                        "type": "object",
                        "properties": {
                            "times": {"type": "integer"},
                            "duration": {"type": "number"},
                            "active_users": {"type": "integer"},
                            "period": {"type": "number"},
                            "tenants": {"type": "integer"},
                            "users_per_tenant": {"type": "integer"},
                            "timeout": {"type": "number"}
                        },
                        "additionalProperties": False
                    }
                },
                "additionalProperties": False
            }
        }
    }
}


class BenchmarkEngine(object):
    """The Benchmark engine class, an instance of which is initialized by the
    Orchestrator with the benchmarks configuration and then is used to execute
    all specified benchmark scnearios.
    .. note::

        Typical usage:
            ...
            benchmark_engine = BenchmarkEngine(config, task)
            # Deploying the cloud...
            # endpoint - is a dict with data on endpoint of deployed cloud
            with benchmark_engine.bind(endpoints):
                benchmark_engine.run()
    """

    def __init__(self, config, task):
        """BenchmarkEngine constructor.
        :param config: The configuration with specified benchmark scenarios
        :param task: The current task which is being performed
        """
        self.config = config
        self.task = task
        self._validate_config()

    @rutils.log_task_wrapper(LOG.info, _("Benchmark configs validation."))
    def _validate_config(self):
        task_uuid = self.task['uuid']
        # Perform schema validation
        try:
            jsonschema.validate(self.config, CONFIG_SCHEMA)
        except jsonschema.ValidationError as e:
            LOG.exception(_('Task %s: Error: %s') % (task_uuid, e.message))
            raise exceptions.InvalidConfigException(message=e.message)

        # Check for benchmark scenario names
        available_scenarios = set(base.Scenario.list_benchmark_scenarios())
        for scenario in self.config:
            if scenario not in available_scenarios:
                LOG.exception(_('Task %s: Error: the specified '
                                'benchmark scenario does not exist: %s') %
                              (task_uuid, scenario))
                raise exceptions.NoSuchScenario(name=scenario)
            # Check for conflicting config parameters
            for run in self.config[scenario]:
                if 'times' in run['config'] and 'duration' in run['config']:
                    message = _("'times' and 'duration' cannot be set "
                                "simultaneously for one continuous "
                                "scenario run.")
                    LOG.exception(_('Task %s: Error: %s') % (task_uuid,
                                                             message))
                    raise exceptions.InvalidConfigException(message=message)
                if ((run.get('execution', 'continuous') == 'periodic' and
                     'active_users' in run['config'])):
                    message = _("'active_users' parameter cannot be set "
                                "for periodic test runs.")
                    LOG.exception(_('Task %s: Error: %s') % (task_uuid,
                                                             message))
                    raise exceptions.InvalidConfigException(message=message)

    def run(self):
        """Runs the benchmarks according to the test configuration
        the benchmark engine was initialized with.

        :returns: List of dicts, each dict containing the results of all the
                  corresponding benchmark test launches
        """
        self.task.update_status(consts.TaskStatus.TEST_TOOL_BENCHMARKING)

        results = {}
        for name in self.config:
            for n, kwargs in enumerate(self.config[name]):
                key = {'name': name, 'pos': n, 'kw': kwargs}
                try:
                    scenario_runner = runner.ScenarioRunner.get_runner(
                                            self.task, self.endpoints, kwargs)
                    result = scenario_runner.run(name, kwargs)
                    self.task.append_results(key, {"raw": result,
                                                   "validation":
                                                   {"is_valid": True}})
                    results[json.dumps(key)] = result
                except exceptions.InvalidScenarioArgument as e:
                    self.task.append_results(key, {"raw": [],
                                                   "validation":
                                                   {"is_valid": False,
                                                    "exc_msg": e.message}})
                    self.task.set_failed()
                    LOG.error(_("Scenario (%(pos)s, %(name)s) input arguments "
                                "validation error: %(msg)s") %
                              {"pos": n, "name": name, "msg": e.message})

        return results

    def bind(self, endpoints):
        self.endpoints = [endpoint.Endpoint(**endpoint_dict)
                          for endpoint_dict in endpoints]
        # NOTE(msdubov): Passing predefined user endpoints hasn't been
        #                implemented yet, so the scenario runner always gets
        #                a single admin endpoint here.
        admin_endpoint = self.endpoints[0]
        admin_endpoint.permission = consts.EndpointPermission.ADMIN
        # Try to access cloud via keystone client
        clients = osclients.Clients(admin_endpoint)
        clients.get_verified_keystone_client()
        return self

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_value, exc_traceback):
        if exc_type is not None:
            self.task.set_failed()
        else:
            self.task.update_status(consts.TaskStatus.FINISHED)
