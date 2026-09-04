"""
Derive a readable boolean expression for a top-level output or a state
element (flip-flop), backward from its driving net, stopping at the nearest
boundary: primary inputs, or *other* state elements (never auto-crossing a
flip-flop into the previous clock cycle - see -l/--expr below).

  python3 derive_expr.py gds/04_final.gds -l
  python3 derive_expr.py gds/04_final.gds --expr S
  python3 derive_expr.py gds/04_final.gds --expr Q541

-l lists every top-level output and every state element (flip-flop),
    named Q<instance-index> (GDS instance names aren't preserved - see
    CLAUDE.md - so the flip-flop's netlist instance index is the only
    stable handle we have; it matches the "#<idx>" shown in
    render_diagram.py's diagrams).

--expr <name> derives one combinational cone:
  - for an output name: expr of that output's driving net, in terms of
    primary inputs and/or Q<idx> leaves (other flip-flops' current state).
  - for a state element Q<idx>: expr of that flip-flop's D pin (its next-
    state logic), ANDed with its RESET_B behavior if resolvable, again in
    terms of primary inputs and/or other Q<idx> leaves.

Neither mode auto-expands a Q<idx> leaf into its own D-logic - that would
mean silently walking backward in time, which for a self-holding register
(D = en ? new : Q, i.e. this design's shift-register mux2) never bottoms
out at primary inputs at all (E=0 forever holds state) without an explicit,
deliberate unroll depth. So this script always derives exactly one
combinational hop per invocation; to walk further back, re-run --expr on
whichever Q<idx> names showed up as leaves. See CLAUDE.md / the "algo-aug-30"
session for why: it's a genuine sequential fixed point, not a shortcut.

Safeguards against algorithmic blowup (see module docstring sections below
for detail): per-net memoization (not per-path - avoids exponential
reconvergent-fanout blowup), iterative traversal with an explicit stack (no
Python recursion-depth limit), a hard node-visit budget, cycle detection
within a single combinational pass, boolean constant-folding, and a
value-sharing (let-bound) printer so a derived expression stays linear in
size instead of re-inlining shared subexpressions at every use site.
"""
import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field

import gdsconn
import render_diagram as rd


# ---------------------------------------------------------------------------
# Boolean expression AST - frozen/hashable so structurally-identical nodes
# compare equal, which the let-binding printer relies on to spot sharing.
# ---------------------------------------------------------------------------

class Expr:
    pass


@dataclass(frozen=True)
class Const(Expr):
    bit: int


@dataclass(frozen=True)
class Var(Expr):
    name: str  # primary-input IO pin name


@dataclass(frozen=True)
class StateRef(Expr):
    idx: int  # flip-flop instance index - printed as Q<idx>


@dataclass(frozen=True)
class Unknown(Expr):
    label: str  # unresolved net (extraction gap) - see CLAUDE.md known gaps


@dataclass(frozen=True)
class Not(Expr):
    x: Expr


@dataclass(frozen=True)
class And(Expr):
    args: tuple


@dataclass(frozen=True)
class Or(Expr):
    args: tuple


@dataclass(frozen=True)
class Xor(Expr):
    args: tuple


@dataclass(frozen=True)
class Ite(Expr):
    cond: Expr
    then: Expr
    els: Expr


ZERO, ONE = Const(0), Const(1)


def children(n):
    if isinstance(n, Not):
        return (n.x,)
    if isinstance(n, (And, Or, Xor)):
        return n.args
    if isinstance(n, Ite):
        return (n.cond, n.then, n.els)
    return ()


def substitute_expr(root, *, vars=None, states=None, unknowns=None):
    """Substitute leaves in an Expr DAG and rebuild through the smart
    constructors.  This is intentionally iterative and memoized, like the
    net traversal itself, so sequential unrolling can reuse shared values
    without recursively re-deriving logic."""
    vars = vars or {}
    states = states or {}
    unknowns = unknowns or {}
    memo = {}
    stack = [(root, False)]
    while stack:
        node, expanded = stack.pop()
        if node in memo:
            continue
        if not expanded:
            stack.append((node, True))
            stack.extend((child, False) for child in children(node) if child not in memo)
            continue
        if isinstance(node, Const):
            value = node
        elif isinstance(node, Var):
            value = vars.get(node.name, node)
        elif isinstance(node, StateRef):
            value = states.get(node.idx, node)
        elif isinstance(node, Unknown):
            value = unknowns.get(node.label, node)
        elif isinstance(node, Not):
            value = mk_not(memo[node.x])
        elif isinstance(node, And):
            value = mk_and(memo[arg] for arg in node.args)
        elif isinstance(node, Or):
            value = mk_or(memo[arg] for arg in node.args)
        elif isinstance(node, Xor):
            args = [memo[arg] for arg in node.args]
            value = ZERO
            for arg in args:
                value = mk_xor2(value, arg)
        elif isinstance(node, Ite):
            value = mk_ite(memo[node.cond], memo[node.then], memo[node.els])
        else:
            raise TypeError(f"unsupported Expr node {node!r}")
        memo[node] = value
    return memo[root]


def evaluate_expr(root, values):
    """Evaluate a Const/Var-only Expr DAG with ``values`` mapping variable
    names to 0/1.  StateRef and Unknown leaves are rejected so a validation
    cannot accidentally treat an incomplete unroll as a Boolean result."""
    memo = {}
    stack = [(root, False)]
    while stack:
        node, expanded = stack.pop()
        if node in memo:
            continue
        if not expanded:
            stack.append((node, True))
            stack.extend((child, False) for child in children(node) if child not in memo)
            continue
        if isinstance(node, Const):
            value = node.bit
        elif isinstance(node, Var):
            if node.name not in values:
                raise KeyError(f"no value for variable {node.name!r}")
            value = int(bool(values[node.name]))
        elif isinstance(node, (StateRef, Unknown)):
            raise ValueError(f"expression was not fully resolved: {node!r}")
        elif isinstance(node, Not):
            value = 1 - memo[node.x]
        elif isinstance(node, And):
            value = int(all(memo[arg] for arg in node.args))
        elif isinstance(node, Or):
            value = int(any(memo[arg] for arg in node.args))
        elif isinstance(node, Xor):
            value = 0
            for arg in node.args:
                value ^= memo[arg]
        elif isinstance(node, Ite):
            value = memo[node.then] if memo[node.cond] else memo[node.els]
        else:
            raise TypeError(f"unsupported Expr node {node!r}")
        memo[node] = value
    return memo[root]


# --- smart constructors: constant-fold + flatten + de-dup as we build ------

def mk_not(x):
    if isinstance(x, Const):
        return Const(1 - x.bit)
    if isinstance(x, Not):
        return x.x
    return Not(x)


def _flatten_dedup(cls, args, absorb_bit, identity_bit):
    flat = []
    seen = set()
    for a in args:
        if isinstance(a, Const):
            if a.bit == absorb_bit:
                return Const(absorb_bit)
            continue  # identity element, drop
        for sub in (a.args if isinstance(a, cls) else (a,)):
            if sub not in seen:
                seen.add(sub)
                flat.append(sub)
    if not flat:
        return Const(identity_bit)
    if len(flat) == 1:
        return flat[0]
    return cls(tuple(flat))


def mk_and(args):
    return _flatten_dedup(And, args, absorb_bit=0, identity_bit=1)


def mk_or(args):
    return _flatten_dedup(Or, args, absorb_bit=1, identity_bit=0)


def mk_xor2(a, b):
    if isinstance(a, Const):
        return b if a.bit == 0 else mk_not(b)
    if isinstance(b, Const):
        return a if b.bit == 0 else mk_not(a)
    if a == b:
        return ZERO
    return Xor((a, b))


def mk_ite(cond, then, els):
    if isinstance(cond, Const):
        return then if cond.bit == 1 else els
    if then == els:
        return then
    return Ite(cond, then, els)


# ---------------------------------------------------------------------------
# Tiny parser/evaluator for gate-name-map.yaml's `expr` field, e.g.
# "Y = !(A & B)", "X = (A1 & A2) | !B1_N", "X = S ? A1 : A0". Parsing the
# yaml's own expr text (rather than hand-duplicating each gate's logic in
# Python) keeps one source of truth - and doubles as a self-check: loading
# validates every identifier in an expr against that gate's declared
# `inputs`, which is exactly the check that would have caught the
# AND4_2N/AO21_NB/AOI21_NB name-mismatch bugs fixed in the yaml alongside
# this script (expr referenced the pre-bubble pin name, e.g. "B1", instead
# of the actual declared "_N"-suffixed pin the netlist wires against).
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(r"\s*(=>|[A-Za-z_][A-Za-z0-9_]*|[01]\b|[!&|^()?:=])")


def tokenize(s):
    pos = 0
    toks = []
    while pos < len(s):
        m = TOKEN_RE.match(s, pos)
        if not m or m.end() == pos:
            if s[pos:].strip() == "":
                break
            raise ValueError(f"cannot tokenize expr at {s[pos:]!r} in {s!r}")
        pos = m.end()
        tok = m.group(1)
        toks.append(tok)
    return toks


class ExprParser:
    """Recursive-descent parser over a fixed grammar; precedence low->high:
    ternary  >  |  >  ^  >  &  >  unary !  >  atom."""

    def __init__(self, toks):
        self.toks = toks
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def eat(self, tok=None):
        t = self.peek()
        if tok is not None and t != tok:
            raise ValueError(f"expected {tok!r}, got {t!r}")
        self.i += 1
        return t

    def parse(self):
        node = self.ternary()
        if self.i != len(self.toks):
            raise ValueError(f"trailing tokens: {self.toks[self.i:]}")
        return node

    def ternary(self):
        cond = self.or_()
        if self.peek() == "?":
            self.eat("?")
            then = self.or_()
            self.eat(":")
            els = self.ternary()
            return ("ite", cond, then, els)
        return cond

    def or_(self):
        node = self.xor()
        while self.peek() == "|":
            self.eat("|")
            node = ("or", node, self.xor())
        return node

    def xor(self):
        node = self.and_()
        while self.peek() == "^":
            self.eat("^")
            node = ("xor", node, self.and_())
        return node

    def and_(self):
        node = self.unary()
        while self.peek() == "&":
            self.eat("&")
            node = ("and", node, self.unary())
        return node

    def unary(self):
        if self.peek() == "!":
            self.eat("!")
            return ("not", self.unary())
        return self.atom()

    def atom(self):
        if self.peek() == "(":
            self.eat("(")
            node = self.ternary()
            self.eat(")")
            return node
        t = self.peek()
        if t in ("0", "1"):
            self.eat()
            return ("const", int(t))
        if t is None or not re.match(r"^[A-Za-z_]", t):
            raise ValueError(f"expected identifier or '(', got {t!r}")
        self.eat()
        return ("var", t)


def parse_gate_expr(expr_str):
    """'X = A & !B' -> (output_pin_name, parsed_ast)."""
    lhs, _, rhs = expr_str.partition("=")
    out_pin = lhs.strip()
    ast = ExprParser(tokenize(rhs)).parse()
    return out_pin, ast


def eval_ast(ast, env):
    """env: {identifier -> Expr}. Uses the mk_* smart constructors so
    substitution constant-folds as it goes."""
    kind = ast[0]
    if kind == "const":
        return Const(ast[1])
    if kind == "var":
        name = ast[1]
        if name not in env:
            raise KeyError(name)
        return env[name]
    if kind == "not":
        return mk_not(eval_ast(ast[1], env))
    if kind == "and":
        return mk_and([eval_ast(ast[1], env), eval_ast(ast[2], env)])
    if kind == "or":
        return mk_or([eval_ast(ast[1], env), eval_ast(ast[2], env)])
    if kind == "xor":
        return mk_xor2(eval_ast(ast[1], env), eval_ast(ast[2], env))
    if kind == "ite":
        return mk_ite(eval_ast(ast[1], env), eval_ast(ast[2], env), eval_ast(ast[3], env))
    raise ValueError(f"unhandled ast node {ast!r}")


def build_and_validate_gate_map(path):
    """Load gate-name-map.yaml and parse+self-check every gate's `expr`
    against its declared inputs/outputs. Fails fast at startup (not
    mid-traversal) if the yaml and its own inputs/outputs list disagree -
    exactly the class of bug fixed in AND4_2N/AO21_NB/AOI21_NB.

    `expr` is normally one string ("X = ..."), but a handful of cells (conb's
    HI/LO tie-off) drive more than one independent output from one instance,
    so it may instead be a {out_pin: "OUT_PIN = ..."} mapping - either way
    this builds g["_ast_by_pin"], keyed by the pin the expr's own LHS names."""
    gate_map = rd.load_gate_map(path)
    for raw, g in gate_map.items():
        expr = g.get("expr")
        if not expr:
            continue  # registers/clock/mux/physical_only: no boolean expr
        expr_strs = expr.values() if isinstance(expr, dict) else (expr,)
        outputs = g.get("outputs", [])
        ast_by_pin = {}
        for expr_str in expr_strs:
            out_pin, ast = parse_gate_expr(expr_str)
            if outputs and out_pin not in outputs:
                raise ValueError(
                    f"gate {g['name']!r}: expr LHS {out_pin!r} not in declared outputs {outputs}"
                )
            # dry-run substitution with each declared input mapped to itself,
            # to confirm every identifier the expr references is declared.
            probe_env = {p: Var(p) for p in g.get("inputs", [])}
            try:
                eval_ast(ast, probe_env)
            except KeyError as e:
                raise ValueError(
                    f"gate {g['name']!r} ({raw}): expr {expr_str!r} references "
                    f"undeclared pin {e.args[0]!r}; declared inputs are {g.get('inputs')}"
                )
            ast_by_pin[out_pin] = ast
        g["_ast_by_pin"] = ast_by_pin
    return gate_map


# ---------------------------------------------------------------------------
# Netlist-level classification and traversal
# ---------------------------------------------------------------------------

class BlowupError(Exception):
    pass


class CombinationalCycleError(Exception):
    pass


def gate_info(netlist, gate_map, idx):
    return rd.gate_info(netlist.insts[idx].cell.name, gate_map)


def net_unresolved_label(netlist, net):
    return rd.net_label(netlist, net)  # reuses render_diagram's io-pin/blob labeling


class Design:
    """Precomputed once per netlist: which IO nets are inputs vs. outputs,
    and which instances are state elements (flip-flops)."""

    def __init__(self, netlist, gate_map):
        self.netlist = netlist
        self.gate_map = gate_map
        self.io_output_name = {}  # net -> name
        self.io_input_name = {}  # net -> name
        self.state_insts = []  # sorted list of flip-flop instance indices

        driven_nets = set()
        for idx, inst in enumerate(netlist.insts):
            if netlist.inst_kind[idx] != "logic":
                continue
            info = gate_info(netlist, gate_map, idx)
            if not info:
                continue
            if info.get("category") == "register":
                self.state_insts.append(idx)
            for pin in info.get("outputs", []):
                net = netlist.pin_net.get((idx, pin))
                if net is not None:
                    driven_nets.add(net)
        self.state_insts.sort()

        for name, net in netlist.io_net.items():
            if name in ("VPWR", "VGND"):
                continue  # power strap, labeled like a signal pin but not logic
            if net in driven_nets:
                self.io_output_name[net] = name
            else:
                self.io_input_name[net] = name

    def driver_of(self, net):
        """Classify what drives `net`: ('input', name) | ('state', idx) |
        ('gate', idx, info, out_pin) | ('unresolved',). A net with more than
        one instance-output endpoint indicates a multi-driver bug in the
        extraction/design; surfaced rather than silently picking one. The
        driving out_pin is carried through (not just the instance) because a
        multi-output cell - e.g. conb's HI/LO tie cell - has one expr per
        output, not one per instance."""
        if net in self.io_input_name:
            return ("input", self.io_input_name[net])
        drivers = []
        for (kind, idx, pin) in self.netlist.net_endpoints.get(net, []):
            if kind != "inst":
                continue
            info = gate_info(self.netlist, self.gate_map, idx)
            if info and pin in info.get("outputs", []):
                drivers.append((idx, info, pin))
        if not drivers:
            return ("unresolved",)
        if len(drivers) > 1:
            raise ValueError(f"net {net} has {len(drivers)} instance-output drivers - multi-driver net, extraction bug?")
        idx, info, pin = drivers[0]
        if info.get("category") == "register":
            return ("state", idx)
        if info.get("category") in ("clock", "physical_only") or pin not in info.get("_ast_by_pin", {}):
            return ("unresolved",)
        return ("gate", idx, info, pin)


def compute_expr(design, root_net, node_budget):
    """Iterative (explicit-stack) post-order evaluation of the combinational
    cone feeding root_net, memoized per net. Stops at primary inputs and at
    state-element (flip-flop) Q outputs - never crosses a flip-flop.

    Returns (expr, state_refs_used, unresolved_nets).
    """
    netlist = design.netlist
    memo = {}
    state_refs = set()
    unresolved = set()
    on_stack = set()
    nodes_visited = 0

    stack = [(root_net, False)]
    while stack:
        net, expanded = stack.pop()
        if net in memo:
            continue
        if not expanded:
            if net in on_stack:
                raise CombinationalCycleError(
                    f"combinational cycle detected at net {net} - unexpected in a "
                    f"valid synchronous design; likely an extraction gap (see "
                    f"CLAUDE.md known rough edges) misread as a real driver"
                )
            nodes_visited += 1
            if nodes_visited > node_budget:
                raise BlowupError(
                    f"exceeded --max-nodes={node_budget} while expanding net {net}; "
                    f"this fan-in cone is either huge or (more likely) an "
                    f"extraction/classification bug is making the walk over-reach - "
                    f"sanity check with -l first, or raise --max-nodes deliberately"
                )
            on_stack.add(net)
            driver = design.driver_of(net)
            stack.append((net, True))  # finalize marker goes on first, so it
            # sits *under* this net's children and pops only after they do
            if driver[0] == "gate":
                _, idx, info, _out_pin = driver
                for pin in info.get("inputs", []):
                    child = netlist.pin_net.get((idx, pin))
                    if child is not None and child not in memo:
                        stack.append((child, False))
        else:
            on_stack.discard(net)
            driver = design.driver_of(net)
            if driver[0] == "input":
                memo[net] = Var(driver[1])
            elif driver[0] == "state":
                state_refs.add(driver[1])
                memo[net] = StateRef(driver[1])
            elif driver[0] == "unresolved":
                unresolved.add(net)
                memo[net] = Unknown(net_unresolved_label(netlist, net))
            else:
                _, idx, info, out_pin = driver
                env = {}
                for pin in info.get("inputs", []):
                    child = netlist.pin_net.get((idx, pin))
                    env[pin] = memo[child] if child is not None else Unknown(f"{info['name']}#{idx}.{pin}")
                memo[net] = eval_ast(info["_ast_by_pin"][out_pin], env)

    return memo[root_net], state_refs, unresolved


def state_element_expr(design, idx, node_budget):
    """Next-state expr for flip-flop `idx`: async_ctrl_pin ? D : effect,
    folding away the async term if that pin can't be resolved (known
    extraction gap - CLAUDE.md notes dfrtp.RESET_B pins historically came
    back unresolved, most plausibly tied to a constant power rail).

    Not every register type has the same async control pin: dfrtp has an
    active-low RESET_B (clears to 0), dfstp has an active-low SET_B (presets
    to 1, see gate-name-map.yaml's `async_ctrl`), and dfxtp has neither. The
    pin name and its asserted-state effect both come from the gate map
    rather than being hardcoded, so this one function covers all three."""
    netlist = design.netlist
    d_net = netlist.pin_net.get((idx, "D"))
    if d_net is None:
        raise ValueError(f"Q{idx}: D pin unresolved in extraction, cannot derive")
    d_expr, state_refs, unresolved = compute_expr(design, d_net, node_budget)

    info = gate_info(netlist, design.gate_map, idx)
    ctrl = info.get("async_ctrl") if info else None
    reset_note = None
    if ctrl is None:
        final = d_expr
        reset_note = "this register type has no async control pin - D flows through unconditionally"
    else:
        ctrl_net = netlist.pin_net.get((idx, ctrl["pin"]))
        if ctrl_net is None:
            final = d_expr
            reset_note = f"{ctrl['pin']} pin not found in extraction - treating as never-asserted"
        else:
            c_driver = design.driver_of(ctrl_net)
            if c_driver[0] == "unresolved":
                final = d_expr
                reset_note = (
                    f"{ctrl['pin']} unresolved (known extraction gap, see CLAUDE.md - likely "
                    f"tied to a constant power rail) - treating as never-asserted for this expression"
                )
            else:
                c_expr, c_state_refs, c_unresolved = compute_expr(design, ctrl_net, node_budget)
                state_refs |= c_state_refs
                unresolved |= c_unresolved
                effect = ONE if ctrl.get("effect", 0) else ZERO
                final = mk_ite(c_expr, d_expr, effect)

    return final, state_refs, unresolved, reset_note


# ---------------------------------------------------------------------------
# DAG-aware ("let"-bound) printer: any compound subexpression used more than
# once gets a name and is printed once, instead of being re-inlined at every
# use site - keeps output linear in the number of *distinct* subexpressions
# even though the underlying value (e.g. a shared register-equality term
# feeding several AND-reduce gates) fans out to many places.
# ---------------------------------------------------------------------------

def refcounts(root):
    counts = defaultdict(int)
    stack = [root]
    first_visit = set()
    while stack:
        n = stack.pop()
        counts[n] += 1
        if n in first_visit:
            continue
        first_visit.add(n)
        stack.extend(children(n))
    return counts


def render_expr(root, target_name):
    counts = refcounts(root)
    is_leaf = lambda n: isinstance(n, (Const, Var, StateRef, Unknown))
    let_of = {}
    lines = []

    def label(n):
        if isinstance(n, Const):
            return "1" if n.bit else "0"
        if isinstance(n, Var):
            return n.name
        if isinstance(n, StateRef):
            return f"Q{n.idx}"
        if isinstance(n, Unknown):
            return f"UNRESOLVED[{n.label}]"
        return let_of[n]

    def text(n):
        if n in let_of:
            return let_of[n]
        if isinstance(n, Not):
            inner = n.x
            return f"!{label(inner)}" if is_leaf(inner) or inner in let_of else f"!({text(inner)})"
        if isinstance(n, And):
            return "(" + " & ".join(label(a) if is_leaf(a) or a in let_of else text(a) for a in n.args) + ")"
        if isinstance(n, Or):
            return "(" + " | ".join(label(a) if is_leaf(a) or a in let_of else text(a) for a in n.args) + ")"
        if isinstance(n, Xor):
            return "(" + " ^ ".join(label(a) if is_leaf(a) or a in let_of else text(a) for a in n.args) + ")"
        if isinstance(n, Ite):
            parts = [n.cond, n.then, n.els]
            c, t, e = (label(p) if is_leaf(p) or p in let_of else text(p) for p in parts)
            return f"({c} ? {t} : {e})"
        return label(n)

    # post-order: bind names to any compound node used >1 time, deepest first
    def postorder(n, seen):
        if n in seen:
            return
        seen.add(n)
        for c in children(n):
            postorder(c, seen)
        order.append(n)

    order = []
    postorder(root, set())

    next_id = [0]
    for n in order:
        if is_leaf(n) or n is root:
            continue
        if counts[n] > 1:
            name = f"e{next_id[0]}"
            next_id[0] += 1
            lines.append(f"{name} = {text(n)}")
            let_of[n] = name

    lines.append(f"{target_name} = {label(root) if is_leaf(root) or root in let_of else text(root)}")
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

STATE_NAME_RE = re.compile(r"^Q(\d+)$")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gds", nargs="?", default="gds/04_final.gds")
    ap.add_argument("--gate-map", default="docs/gate-name-map.yaml")
    ap.add_argument("-l", "--list", action="store_true", help="list all outputs and state elements, then exit")
    ap.add_argument("--expr", metavar="NAME", help="derive an expression for this output or state element (Q<idx>)")
    ap.add_argument("--max-nodes", type=int, default=200_000, help="abort if a single combinational pass visits more nets than this (default: %(default)s)")
    ap.add_argument("-o", "--out", default=None, help="write result to a file instead of stdout")
    args = ap.parse_args()

    if not args.list and not args.expr:
        ap.error("pass -l to list names, or --expr NAME to derive an expression")

    gate_map = build_and_validate_gate_map(args.gate_map)
    netlist = gdsconn.extract(args.gds)
    design = Design(netlist, gate_map)

    out_lines = []

    if args.list:
        out_lines.append(f"outputs ({len(design.io_output_name)}):")
        for name in sorted(design.io_output_name.values()):
            out_lines.append(f"  {name}")
        out_lines.append(f"state elements ({len(design.state_insts)}):")
        for idx in design.state_insts:
            out_lines.append(f"  Q{idx}")
        out_lines.append(f"primary inputs ({len(design.io_input_name)}):")
        for name in sorted(design.io_input_name.values()):
            out_lines.append(f"  {name}")

    if args.expr:
        name = args.expr
        m = STATE_NAME_RE.match(name)
        try:
            if m:
                idx = int(m.group(1))
                if idx not in design.state_insts:
                    sys.exit(f"[error] {name!r} is not a known state element; run -l to see valid names")
                expr, state_refs, unresolved, reset_note = state_element_expr(design, idx, args.max_nodes)
                target_label = name
                if reset_note:
                    out_lines.append(f"# note: {reset_note}")
            elif name in design.io_output_name.values():
                net = next(n for n, nm in design.io_output_name.items() if nm == name)
                expr, state_refs, unresolved = compute_expr(design, net, args.max_nodes)
                target_label = name
            else:
                sys.exit(f"[error] {name!r} is not a known output or state element; run -l to see valid names")
        except (BlowupError, CombinationalCycleError, ValueError) as e:
            sys.exit(f"[error] {e}")

        out_lines.extend(render_expr(expr, target_label))
        if state_refs:
            out_lines.append(f"# references {len(state_refs)} state element(s): " + ", ".join(f"Q{i}" for i in sorted(state_refs)))
        if unresolved:
            out_lines.append(f"# {len(unresolved)} unresolved net(s) (extraction gap) folded in as UNRESOLVED[...] leaves - see CLAUDE.md known rough edges")

    text = "\n".join(out_lines)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
        print(f"[info] wrote {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
