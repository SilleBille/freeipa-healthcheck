#
# Copyright (C) 2019 FreeIPA Contributors see COPYING for license
#

import argparse
import logging
import pkg_resources
import sys

from ipahealthcheck.core.plugin import Result, Results
from ipahealthcheck.core.output import output_registry
from ipahealthcheck.core import constants
from ipahealthcheck.meta.services import ServiceCheck


logging.basicConfig(format='%(message)s')
logger = logging.getLogger()


def find_registries():
    return {
        ep.name: ep.resolve()
        for ep in pkg_resources.iter_entry_points('ipahealthcheck.registry')
    }


def find_plugins(name, registry):
    for ep in pkg_resources.iter_entry_points(name):
        # load module
        ep.load()
    return registry.get_plugins()


def run_plugin(plugin, available=()):
    try:
        result = plugin.check()
        if type(result) not in (Result, Results):
            # Treat no result as success
            result = Result(plugin, constants.SUCCESS)
    except Exception as e:
        logger.debug('Exception raised: %s', e)
        result = Result(plugin, constants.CRITICAL, exception=str(e))

    return result


def source_or_check_matches(plugin, source, check):
    """Determine whether a given a plugin matches if a source
       and optional check are provided.
    """
    if source is not None and plugin.__module__ != source:
        return False

    if check and plugin.__class__.__name__ != check:
        return False

    return True


def run_service_plugins(plugins, source, check):
    """Execute plugins with the base class of ServiceCheck

       This is a specialized check to use systemd to determine
       if a service is running or not.
    """
    results = Results()
    available = []

    for plugin in plugins:
        if not isinstance(plugin, ServiceCheck):
            continue

        if not source_or_check_matches(plugin, source, check):
            continue

        logger.debug('Calling check %s' % plugin)
        result = run_plugin(plugin)

        if result.severity == constants.SUCCESS:
            available.append(plugin.service_name)

        if isinstance(result, Result):
            results.add(result)
        elif isinstance(result, Results):
            results.extend(result)

    return results, set(available)


def run_plugins(plugins, available, source, check):
    """Execute plugins without the base class of ServiceCheck

       These are the remaining, non-service checking checks
       that do validation for various parts of a system.
    """
    results = Results()

    for plugin in plugins:
        if isinstance(plugin, ServiceCheck):
            continue

        if not source_or_check_matches(plugin, source, check):
            continue

        logger.debug('Calling check %s' % plugin)
        # TODO: make this not the default
        if not set(plugin.requires).issubset(available):
            result = Result(plugin, constants.ERROR,
                            msg='%s service(s) not running' %
                            (', '.join(set(plugin.requires) - available)))
        else:
            result = run_plugin(plugin, available)

        if isinstance(result, Result):
            results.add(result)
        elif isinstance(result, Results):
            results.extend(result)

    return results


def parse_options(output_registry):
    output_names = [plugin.__name__.lower() for
                    plugin in output_registry.plugins]
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', dest='debug', action='store_true',
                        default=False, help='Include debug output')
    parser.add_argument('--source', dest='source',
                        default=None,
                        help='Source of checks, e.g. ipahealthcheck.foo.bar')
    parser.add_argument('--check', dest='check',
                        default=None,
                        help='Check to execute, e.g. BazCheck')
    parser.add_argument('--output-type', dest='output', choices=output_names,
                        default='json', help='Output method')
    parser.add_argument('--failures-only', dest='failures_only',
                        action='store_true', default=False,
                        help='Exclude SUCCESS severity on output')
    for plugin in output_registry.plugins:
        onelinedoc = plugin.__doc__.split('\n\n', 1)[0].strip()
        group = parser.add_argument_group(plugin.__name__.lower(),
                                          onelinedoc)
        for option in plugin.options:
            group.add_argument(option[0], **option[1])

    options = parser.parse_args()

    # Validation
    if options.check and not options.source:
        print("--source is required when --check is used")
        sys.exit(1)

    return options


def main():
    framework = object()
    plugins = []
    output = constants.DEFAULT_OUTPUT

    logger.setLevel(logging.INFO)

    options = parse_options(output_registry)

    if options.debug:
        logger.setLevel(logging.DEBUG)

    for name, registry in find_registries().items():
        try:
            registry.initialize(framework)
        except Exception as e:
            print("Unable to initialize %s: %s" % (name, e))
            sys.exit(1)
        for plugin in find_plugins(name, registry):
            plugins.append(plugin)

    for out in output_registry.plugins:
        if out.__name__.lower() == options.output:
            output = out(options)

    if not output.output_only:
        results, available = run_service_plugins(plugins, options.source,
                                                 options.check)
        results.extend(run_plugins(plugins, available, options.source,
                                   options.check))
    else:
        results = None

    try:
        output.render(results)
    except Exception as e:
        logger.error('Output raised %s: %s', e.__class__.__name__, e)