import ast
from itertools import chain
from types import ModuleType
from typing import List

import jedi
from _pytest.python import Function
from jedi.api.classes import Name as JediName

from testmon.testmon_core import TestmonCollector, cached_relpath


class FunctionParser(ast.NodeVisitor):
    def __init__(self, function: ast.FunctionDef):
        self.function = function

    def visit_FunctionDef(self, node):
        if node.name == self.function:
            self.found_function = node
        self.generic_visit(node)

    def get_function_vars(self):
        variable_collector = VariableCollector()
        for stmt in chain(self.function.body, \
                          self.function.args.args):
            variable_collector.visit(stmt)

        return list(variable_collector.read_variables)

class VariableCollector(ast.NodeVisitor):
    def __init__(self):
        self.read_variables = set()
        self.inner_variables = set()
    
    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node not in self.inner_variables:
            self.read_variables.add(node)
        elif isinstance(node.ctx, ast.Store) and node not in self.read_variables:
            self.inner_variables.add(node)
        self.generic_visit(node)
    
    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            self.read_variables.add(node)

def parse_module(module_path):
    with open(module_path, 'r') as file:
        source = file.read()
    parsed = ast.parse(source)
    
    functions: dict[str, ast.FunctionDef] = {}
    for node in parsed.body:
        if isinstance(node, ast.FunctionDef):
            functions[node.name] = node

    stmts: dict[int, ast.stmt] = {}
    for node in ast.walk(parsed):
        if isinstance(node, ast.stmt) and hasattr(node, 'lineno'):
            stmts[node.lineno] = node

    return parsed, functions, stmts

def check_module(collector: TestmonCollector, module_name: str):
    relfilename = cached_relpath(module_name, collector.rootdir)

    if relfilename not in collector._parsed_modules:
        parsed_module, functions, stmts = parse_module(module_name)
        script = jedi.Script(path=module_name)
        collector._parsed_modules[relfilename] = (parsed_module, functions, stmts, script)
    else:
        parsed_module, functions, stmts, script = collector._parsed_modules[relfilename]
    return parsed_module, functions, stmts, script

def parse_function_from_module(tree: ast.Module, functions: dict[str, ast.FunctionDef], function_name: str):
    function = functions.get(function_name, None)
    if function is None:
        return []
    function_parser = FunctionParser(function)
    function_parser.visit(tree)

    return function_parser.get_function_vars()

def get_defs(script: jedi.Script, vars: list[ast.Name | ast.Attribute]):
    return [script.goto(line=rep.lineno,\
                        column=rep.end_col_offset,\
                        follow_imports=True,\
                        follow_builtin_imports=False) for rep in \
                        vars]

def add_transitive_defs(collector: TestmonCollector, curr_defs: set[JediName], old_defs: set[JediName]):
    new_defs: set[JediName] = set()
    for name in curr_defs:
        _, _, stmts, script = check_module(collector, name.module_path)

        stmt = stmts.get(name.line, None)
        if stmt is not None:
            variable_collector = VariableCollector()
            variable_collector.visit(stmt)
            found_defs: List[List[JediName]] = get_defs(script=script,
                                                        vars=list(variable_collector.read_variables))

            new_defs |= set([de for subdefs in found_defs for de in subdefs if \
                                    not de.module_path._str.endswith('.pyi') and \
                                    collector.cov._check_include_omit_etc(de.module_path._str, None)])
    old_defs |= curr_defs
    new_defs -= old_defs
    if new_defs:
        add_transitive_defs(collector, new_defs, old_defs)

def static_analysis(collector: TestmonCollector, item: Function):
    if collector.cov is None:
        collector.setup_coverage()

    module: ModuleType = item.module
    if module is None:
        return

    parsed_module, functions, _, script = check_module(collector, module.__file__)
    vars = parse_function_from_module(parsed_module, functions, item.name)
    defs: List[List[JediName]] = get_defs(script=script, vars=vars)

    defs: set[JediName] = set([de for subdefs in defs for de in subdefs if \
                            not de.module_path._str.endswith('.pyi') and \
                            collector.cov._check_include_omit_etc(de.module_path._str, None)])

    add_transitive_defs(collector, defs, defs)
    defs = list(filter(lambda de: isinstance(de.line, int), defs))
    return defs

def add_static_lines(collector: TestmonCollector, item: Function):
    try:
        if not collector._static_analisys:
            return
        defs = static_analysis(collector, item)
        files: dict = collector._static_lines.get(item.nodeid, {})
        for de in defs:
            relfilename = cached_relpath(de.module_path._str, collector.rootdir)
            lines: set = files.get(relfilename, set())
            lines.add(de.line)
            files[relfilename] = lines
        if files:
            collector._static_lines[item.nodeid] = files
    except Exception:
        pass