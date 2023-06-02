#!/usr/bin/env python3
# Copyright (c) 2018-2020 AT&T Intellectual Property. All rights reserved.
#
# SPDX-License-Identifier: GPL-2.0-only

from argparse import ArgumentParser
from collections import OrderedDict
import json
import os
from os import listdir
import shutil
import subprocess
import sys

from vyatta.command import CommandFiller, MISSING_VALUE_TEMPLATE

TEXT_LEAF_LABEL = '@text'
LIST_ELEM_LABEL = '@element'
DICT_ELEM_LABEL = '@dict'
ENTER_LABEL = '@enter'
EXIT_LABEL = '@exit'

DIR_TRAVERSE_UP_LABEL = "¬"

CONFIGS_DIR = '/etc/vyatta-routing/configs'
PRIORITIES_FILENAME = '/priorities.json'
STEPS_FILENAME = '/steps.json'
COMMANDS_DIRNAME = '/commands'
VYATTA_JSON_FILE = "/etc/vyatta-routing/frr.json"
OUTPUT_FILE = "/etc/vyatta-routing/frr.conf"
OUTPUT_FILE_OWNER = "routing"
FRR_RELOAD = "/usr/bin/vtysh"


def print_err(x): return print(x, file=sys.stderr)


class VyattaJSONParser:
    """Traverses the json config, visits every node and extracts the values of
    keys referenced in the commands
    """

    class Step:
        """
        An individual parsing step, defined by an object in steps.json.

        A Step contains a list of static configuration lines and a list
        of SyntaxFile objects. When executed the static configuration is
        appended to the parser output, and the specified syntax files are
        loaded and the configuration parsed against that syntax. Any
        SyntaxFile which has already been used (ie. in a previous step) is
        ignored.
        """

        def __init__(self, lines, translations):
            self._lines = lines
            self._translations = translations

        def execute(self, parser):
            parser.output.extend(self._lines)

            if not self._translations:
                return

            syntax = {}
            for syntax_file in self._translations:
                if not syntax_file.processed:
                    syntax = {**syntax, **syntax_file.load()}
                    syntax_file.processed = True

            parser.syntax = syntax
            parser.parse_config()


    class SyntaxFile:
        """ Represents a single syntax (translation) file """

        def __init__(self, path, name):
            self.processed = False
            self._path = path
            self._name = name

        def load(self):
            with open(os.path.join(self._path, self._name), 'r') as syntax_json:
                return json.load(syntax_json)


    def __init__(self, vyatta_config=None, syntax=None, debug=False):
        self.tree = vyatta_config
        self.syntax = {} if syntax is None else syntax
        self.syntax_files = {}
        self.steps = []
        # holds the parent of each node in the json. Used to traverse up the tree
        self.parent_stack = []
        # holds the CLI commands as a list of strings
        self.output = []
        # enables debugging commands
        self.debug = debug

    def read_vyatta_config(self, path):
        """Reads vyatta json config file"""
        with open(path, 'r') as vyatta_json:
            self.tree = self.decode_vyatta_config(vyatta_json.read())

    def decode_vyatta_config(self, config_string):
        """Decodes the json with OrderedDicts instead of dicts"""
        return json.loads(config_string, object_pairs_hook=OrderedDict)

    def discover_syntax(self, dir):
        command_files = filter(
            lambda x: x.lower().endswith('.json'), listdir(dir))
        self.syntax_files = dict(
            [(x, self.SyntaxFile(dir, x)) for x in command_files])

    def read_syntax_files(self, dir_path):
        """Reads all files syntax files and merges them to one big
        dict with all the syntax commands"""
        self.syntax = {}
        self.discover_syntax(dir_path)
        for syntax_file in self.syntax_files:
            self.syntax = {**self.syntax, **syntax_file.load()}

    def load_steps(self, path):
        with open(path, 'r') as f:
            steps = json.load(f)

        for step in steps:
            syntax = step.get("translate", [])
            if syntax == "remaining":
                # Special case - use all discovered syntax files.
                # When the step is executed we ignore any files which
                # have already been processed, ie. we will only process
                # any remaining syntax files.
                syntax = self.syntax_files.values()
            else:
                syntax = [self.syntax_files[f] for f in syntax]
            self.steps.append(self.Step(step.get("config", []), syntax))

    def execute_steps(self):
        for step in self.steps:
            self.parent_stack = []
            self.syntax = {}
            step.execute(self)

    def output_config(self, path, owner):
        """Write the already parsed config to a file"""
        # avoid empty file
        self.output.append('!')
        config = '\n'.join(self.output)
        if self.debug:
            print(config)

        try:
            fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY)
            # Ensure permissions are set on an existing file
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as write_file:
                write_file.write(config)
        except Exception as e:
            print_err("Failed to write configuration: {}".format(e))
            return

        try:
            shutil.chown(path, owner, owner)
        except Exception as e:
            print_err("Failed to set configuration file owner: {}".format(e))

    def prioritize(self, filepath):
        """Reads priorities file and sorts the configuration tree"""
        with open(filepath, 'r') as priorities_json:
            priorities = json.load(priorities_json)
        self.sort_tree(priorities)

    def sort_tree(self, priorities):
        """Sorts the vyatta config according to the priorities dict"""
        for node, path in self.depth_first_traverse(self.tree):
            defined_priorities = priorities.get(path, {})
            first_keys = defined_priorities.get('first', [])
            last_keys = defined_priorities.get('last', [])
            # reverse first to sort keys the same way as defined in first list
            for key in reversed(first_keys):
                if key in node:
                    node.move_to_end(key, False)
            for key in last_keys:
                if key in node:
                    node.move_to_end(key, True)
        # clear commands triggering from traversing the tree
        self.output.clear()

    def _process_commands(self, node, path):
        for command in self.retrieve_commands(path):
            # command template exists for this node
            commandf = CommandFiller(command, self.debug)
            pattern_values = commandf.find_all_path_refs()
            # print(command, pattern_values)
            pattern_values = self.retrieve_values(node, pattern_values)
            command = commandf.fill_command(pattern_values)
            if command != '':
                self.output.append(command)

    def parse_config(self):
        """Retrieves each node's command and puts it in the output CLI config
        Returns list of command strings.
        """
        for node, path in self.depth_first_traverse(self.tree):
            self.on_enter(node, path)
            self._process_commands(node, path)
        return self.output

    def retrieve_commands(self, path):
        """Retrieves the command(s) associated with this path, if any"""
        commands = self.syntax.get(path, [])
        if isinstance(commands, str):
            commands = [commands]
        # make references valid identifier names to allow them to be treated by string formatter
        return map(lambda x: x.replace('..', DIR_TRAVERSE_UP_LABEL), commands)

    def depth_first_traverse(self, node, path=''):
        """Traverses the tree in a DFS style returning every node and its path.
        Stores the parent in the stack at each level of recursion and pops it out when done.
        Creates the path of each node incrementally.
        """
        if path == '':
            yield(node, '/')
        else:
            yield(node, path)
        self.parent_stack.append(node)
        if isinstance(node, dict):
            for key in node:
                child_path = path+'/' + key
                yield from self.depth_first_traverse(node[key], child_path)
        elif isinstance(node, list):
            for elem in node:
                child_path = path+'/' + LIST_ELEM_LABEL
                yield from self.depth_first_traverse(elem, child_path)
        self.on_exit(node, path)
        self.parent_stack.pop()

    def on_enter(self, node, path):
        """Executed when node is just visited.
        Used for enter commands.
        """
        self._process_commands(node, path+'/'+ENTER_LABEL)

    def on_exit(self, node, path):
        """Executed when we visited node in path and all its children.
        Removes node from the parent stack and checks if there are any exit commands.
        """
        self._process_commands(node, path+'/'+EXIT_LABEL)

    def find_origin_node(self, node, target_steps):
        """Traverses up the tree as many levels as indicated by the steps list,
        removing the backward steps on the way
        """
        levels_up = 0
        while target_steps[0] == DIR_TRAVERSE_UP_LABEL:
            # print(node, targetSteps)
            target_steps.pop(0)
            levels_up += 1
            node = self.parent_stack[-levels_up]

        return node

    def retrieve_values(self, node, paths):
        """Retrieves the values of nodes referenced in paths (if they exist).

        @param node: the node associated with the command whose patterns are in paths
        @param paths: list of relative paths to desired tree nodes / leaves
        @return: list of (path, value) where value is the value which exists in the
        corresponding path or None if nothing's there
        """
        # print(node, paths)
        values = {}
        for path in paths:
            steps_to_value = [
                step for step in path.split('/') if not step == '']
            value = self.retrieve_value(node, steps_to_value)
            if value != MISSING_VALUE_TEMPLATE:
                values[path] = value
        return values

    def recursive_dictionary_swap(self, node):
        """
        The dictionaries can contain dictionaries and previously only the top
        level ordered dictionary was cast to a dictionary. This function
        recursively casts ordered dictionaries into the dictionary type
        """
        for key in node:
            if type(node[key]) is list:
                if type(node[key][0]) is OrderedDict:
                    new_list = []
                    for entry in node[key]:
                        new_list.append(
                            self.recursive_dictionary_swap(dict(entry)))
                    node[key] = new_list
            elif type(node[key]) is OrderedDict:
                node[key] = self.recursive_dictionary_swap(dict(node[key]))
        return node

    def retrieve_value(self, node, target_steps):
        """Retrieves the value of a node referenced by target steps (if exists)

        @param node: the origin node where the relative path starts in
        @param target_steps: the relative path as a list of steps
        @return: value or None if referenced node doesn't exist
        """
        # print(node, target_steps)
        node = self.find_origin_node(node, target_steps)
        step = target_steps.pop(0)
        value = MISSING_VALUE_TEMPLATE
        if step == TEXT_LEAF_LABEL:
            if type(node) in [OrderedDict, list] and self.debug:
                print('Warning:', node, 'is not a leaf')
            # extract text value
            value = node
        elif step == DICT_ELEM_LABEL:
            if type(node) is list:
                new_list = []
                for elem in list(node):
                    if type(elem) is OrderedDict:
                        elem = self.recursive_dictionary_swap(dict(elem))
                    new_list.append(elem)
                node = new_list
            if type(node) is OrderedDict:
                node = self.recursive_dictionary_swap(dict(node))
            # When using dictionaries we need to replace ", " with another
            # symbol as the if conditional code splits on "," which breaks
            # dictionaries.
            value = str(node).replace(", ", "&")
            value = str(value).replace(" ", "")
        else:
            try:
                value = self.retrieve_value(node[step], target_steps)
            except (KeyError, TypeError) as _:
                if self.debug:
                    print('Warning: Couldnt find', step, 'in', node)
        return value


def main():
    parser = ArgumentParser(
        description='Translate the vyatta JSON config to the FRR CLI config')
    parser.add_argument('-i', help='specify input vyatta JSON file location',
                        default=VYATTA_JSON_FILE)
    parser.add_argument(
        '-o', help='specify output file to be created', default=OUTPUT_FILE)
    parser.add_argument('-c', help='specify configuration directory location',
                        default=CONFIGS_DIR)
    parser.add_argument('-d', help='show debugging messages', action='store_const',
                        const=True, default=False)
    parser.add_argument('--no-reload', help='do not trigger FRR reload', action='store_const',
                        const=False, default=True, dest='reload')
    args = parser.parse_args()

    v = VyattaJSONParser(debug=args.d)
    v.read_vyatta_config(args.i)
    v.prioritize(args.c + PRIORITIES_FILENAME)
    v.discover_syntax(args.c + COMMANDS_DIRNAME)
    v.load_steps(args.c + STEPS_FILENAME)
    v.execute_steps()
    v.output_config(args.o, OUTPUT_FILE_OWNER)

    if args.reload:
        ret = subprocess.run([FRR_RELOAD, "-f",
                              "/etc/vyatta-routing/frr.conf"],
                             stdout=sys.stdout, stderr=sys.stderr)
        sys.exit(ret.returncode)


if __name__ == "__main__":
    main()
