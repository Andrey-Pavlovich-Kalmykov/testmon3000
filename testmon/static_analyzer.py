import ast
import jedi
from jedi.api.classes import Name as JediName

from itertools import chain

from _pytest.python import Function
from testmon.testmon_core import cached_relpath, TestmonCollector
from types import ModuleType
from typing import List

def parse_module(module_path):
    with open(module_path, 'r') as file:
        source = file.read()
    return ast.parse(source)

def parse_function_from_module(tree: ast.Module, function_name):
    function_parser = FunctionParser(function_name)
    function_parser.visit(tree)

    return function_parser.get_function_representation()

def get_stmt_by_lineno(tree: ast.Module, target_lineno: int):
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and hasattr(node, 'lineno') and node.lineno == target_lineno:
            return node
    return None

class FunctionParser(ast.NodeVisitor):
    def __init__(self, target_function):
        self.target_function = target_function
        self.found_function = None

    def visit_FunctionDef(self, node):
        # Check if the current function matches the target
        if node.name == self.target_function:
            self.found_function = node
        self.generic_visit(node)

    def get_function_representation(self):
        if not self.found_function:
            return None

        variable_collector = VariableCollector()
        for stmt in chain(self.found_function.body, \
                          self.found_function.args.args):
            variable_collector.visit(stmt)

        return self.found_function, list(variable_collector.read_variables)

class VariableCollector(ast.NodeVisitor):
    def __init__(self):
        self.read_variables = set()
    
    def visit_Name(self, node):
        # Collect variable names that are being read (loaded)
        if isinstance(node.ctx, ast.Load):
            self.read_variables.add(node)
        self.generic_visit(node)
    
    def visit_Attribute(self, node):
        # Also collect attribute accesses (like obj.attr)
        if isinstance(node.ctx, ast.Load):
            self.read_variables.add(node)
        # self.generic_visit(node)

def add_transitive_defs(collector: TestmonCollector, curr_defs: set[JediName], old_defs: set[JediName]):
    new_defs: set[JediName] = set()
    for name in curr_defs:
        relfilename = cached_relpath(str(name.module_path), collector.rootdir)
        if relfilename not in collector._parsed_modules:
            parsed_module = parse_module(str(name.module_path))
            script = jedi.Script(path=str(name.module_path))
            collector._parsed_modules[relfilename] = (parsed_module, script)
        else:
            parsed_module, script = collector._parsed_modules[relfilename]

        stmt = get_stmt_by_lineno(parsed_module, name.line)
        if stmt is not None:
            variable_collector = VariableCollector()
            variable_collector.visit(stmt)
            found_defs: List[List[JediName]] = [script.goto(line=rep.lineno,\
                                                    column=rep.end_col_offset,\
                                                    follow_imports=True,\
                                                    follow_builtin_imports=False) for rep in \
                                                        list(variable_collector.read_variables)]
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

    relfilename = cached_relpath(module.__file__, collector.rootdir)

    if relfilename not in collector._parsed_modules:
        parsed_module = parse_module(module.__file__)
        script = jedi.Script(path=module.__file__)
        collector._parsed_modules[relfilename] = (parsed_module, script)
    else:
        parsed_module, script = collector._parsed_modules[relfilename]
    _, vars = parse_function_from_module(parsed_module, item.name)
    defs: List[List[JediName]] = [script.goto(line=rep.lineno, \
                                              column=rep.end_col_offset, \
                                              follow_imports=True, \
                                              follow_builtin_imports=False) for rep in vars]

    defs: set[JediName] = set([de for subdefs in defs for de in subdefs if \
                            not de.module_path._str.endswith('.pyi') and \
                            collector.cov._check_include_omit_etc(de.module_path._str, None)])

    add_transitive_defs(collector, defs, defs)
    defs = list(filter(lambda de: isinstance(de.line, int), defs))
    return defs

def add_static_lines(collector: TestmonCollector, item: Function):
    try:
        defs = static_analysis(collector, item)
        files: dict = collector._static_lines.get(item.nodeid, {})
        for de in defs:
            relfilename = cached_relpath(de.module_path._str, collector.rootdir)
            lines: set = files.get(relfilename, set())
            lines.add(de.line)
            files[relfilename] = lines
        collector._static_lines[item.nodeid] = files
    except Exception:
        pass