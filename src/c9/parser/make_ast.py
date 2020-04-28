"""Make PNG AST graphs"""

import lark
from .load import file_parser
from .read import ReadLiterals


def make_ast(filename, function, dest_png):
    with open(filename) as f:
        content = f.read()

    parser = file_parser()
    tree = parser.parse(content)
    ast = ReadLiterals().transform(tree)
    for c in ast.children:
        if c.data == "def_" and c.children[0] == function:
            lark.tree.pydot__tree_to_png(c, dest_png)
