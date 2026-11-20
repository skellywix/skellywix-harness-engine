"""Opt-in, local supervisor. The cooperating stdio client cannot grant authority."""
import os
from pathlib import Path

from .neural_inventory import Inventory, KINDS, MAX_NODES
from .settings_catalog import DeskError, load_dependencies
from .settings_desk import SettingsDesk, check_tree, directory_chain, read_file, read_json
from .supervisor_connections import check_connections, parse_connections
from .supervisor_repairs import Repairs, TOOLS as REPAIR_TOOLS

SCHEMA = 'skellywix-supervisor/1'
PAGE_LIMIT = 48
INVENTORY = {
    'name': 'harness_inventory',
    'description': 'List configured harness components. Declarations and cached tools are not live connections.',
    'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': {
        'kind': {'type': 'string', 'enum': list(KINDS)},
        'cursor': {'type': 'integer', 'minimum': 0, 'maximum': MAX_NODES},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': PAGE_LIMIT},
    }},
    'annotations': {'readOnlyHint': True, 'openWorldHint': False},
}


class Supervisor:
    def __init__(self, policy_path: Path, agent: Path, project: Path, user: Path, *, pi_agent: Path):
        if not project.is_dir():
            raise ValueError('Select a local launch workspace for the supervisor.')
        if not policy_path.is_absolute() or '..' in policy_path.parts:
            raise ValueError('Select the supervisor policy by absolute path.')
        desk = SettingsDesk(agent, project, user, os.environ, native=False, pi_agent=pi_agent)
        if any(policy_path.is_relative_to(root) for root in (desk.omp_root, agent, project, pi_agent)):
            raise ValueError('Keep the supervisor policy outside the managed profiles and workspace.')
        try:
            for root in (agent, project, pi_agent):
                if '..' in root.parts:
                    raise ValueError
                directory_chain(root)
            self.policy_path = policy_path
            self.policy_state = read_file(policy_path)
            raw, identity = self.policy_state
            if raw is None or os.name != 'nt' and (identity[6] != os.geteuid() or identity[2] & 0o022):
                raise ValueError
            policy = read_json(raw)
            check_tree(policy, writing=True)
            if (not isinstance(policy, dict) or policy.get('schema') != SCHEMA
                    or policy.keys() - {'schema', 'connections', 'repairs'}):
                raise ValueError
            connections = parse_connections(policy.get('connections', []))
            repairs = policy.get('repairs', [])
        except (OSError, ValueError, RecursionError):
            raise ValueError('The supervisor needs a valid owner-provided policy file.') from None
        if isinstance(repairs, list) and repairs:
            load_dependencies()
        try:
            self.repairs = Repairs(repairs, desk, self.policy_current)
        except (OSError, ValueError, RecursionError):
            raise ValueError('The supervisor needs a valid owner-provided policy file.') from None
        self.inventory = Inventory(agent, project, native=False, user=user, pi_agent=pi_agent)
        self.repairs.auto_apply()
        self.connections = check_connections(connections, self.inventory, self.policy_current)
        self.snapshot = None
        self.tools = (INVENTORY, *REPAIR_TOOLS)

    def policy_current(self):
        try:
            return read_file(self.policy_path) == self.policy_state
        except DeskError:
            return False

    def call(self, name, args):
        if name == 'harness_inventory':
            return self.page(args)
        if name == 'harness_repairs':
            if not isinstance(args, dict) or args:
                raise ValueError('This tool takes no arguments.')
            return self.repairs.status()
        if name in ('harness_apply_repair', 'harness_undo_repair'):
            return self.repairs.act(args, undo=name == 'harness_undo_repair')
        raise ValueError('Unknown supervisor tool.')

    def page(self, args):
        if not isinstance(args, dict) or args.keys() - {'kind', 'cursor', 'limit'}:
            raise ValueError('Supply only a kind, cursor, and page limit.')
        kind = args.get('kind')
        cursor = args.get('cursor', 0)
        limit = args.get('limit', PAGE_LIMIT)
        if (kind is not None and kind not in KINDS or type(cursor) is not int or not 0 <= cursor <= MAX_NODES
                or type(limit) is not int or not 1 <= limit <= PAGE_LIMIT):
            raise ValueError('Select a supported inventory kind and valid page bounds.')
        if cursor == 0:
            data = self.inventory.scan()
            nodes = [{k: node[k] for k in ('id', 'kind', 'name', 'source', 'state')}
                     for node in data['nodes'] if kind is None or node['kind'] == kind]
            self.snapshot = kind, nodes, data['coverage']
        elif self.snapshot is None or self.snapshot[0] != kind or cursor >= len(self.snapshot[1]):
            raise ValueError('Start at page one for that inventory kind.')
        _, nodes, coverage = self.snapshot
        end = cursor + limit
        return {'schema': 'harness-inventory/1', 'coverage': coverage, 'nodes': nodes[cursor:end],
                'connections': self.connections, 'nextCursor': end if end < len(nodes) else None}
