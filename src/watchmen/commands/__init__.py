"""Per-command-group modules pulled out of the 3,000+ line cli.py.

Each module here holds the `cmd_*` functions for one cohesive command
group; cli.py imports them and wires them into the argparse tree. Helper
utilities they share live in watchmen.util and watchmen.ui.
"""
