"""Managed ceilings hold across actual profile scope, including fabricated tool calls."""
from types import SimpleNamespace
import yaml

from agent.managed_tools import allowed_tools, denial
from gateway.run import _profile_runtime_scope
from model_tools import _select_tool_names, handle_function_call
from agent.tool_executor import _unwrap_tool_search_call


def test_profile_ceiling_applies_to_selection_registry_and_inline_dispatch(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    for path, tools in ((a, ['web_search']), (b, ['memory'])):
        path.mkdir()
        (path / 'config.yaml').write_text(yaml.safe_dump({'agent': {'managed_allowed_tools': tools}}))
    for home, allowed, denied in ((a, 'web_search', 'memory'), (b, 'memory', 'web_search'), (a, 'web_search', 'memory')):
        with _profile_runtime_scope(home):
            assert allowed_tools() == {allowed}
            assert _select_tool_names(['web', 'memory', 'terminal', 'file'], None, True) <= {allowed}
            assert denial(allowed) is None
            assert denial(denied)
            for tool in ('terminal', 'read_file', 'execute_code', 'delegate_task', 'need_full_tools', 'tool_call', denied):
                assert _unwrap_tool_search_call(SimpleNamespace(), tool, {})[2]
                assert 'unavailable' in handle_function_call(tool, {})


def test_absent_preserves_personal_tools_explicit_empty_denies_all(tmp_path):
    (tmp_path / 'config.yaml').write_text('agent: {}\n')
    with _profile_runtime_scope(tmp_path):
        assert allowed_tools() is None
        assert denial('terminal') is None
    (tmp_path / 'config.yaml').write_text('agent:\n  managed_allowed_tools: []\n')
    with _profile_runtime_scope(tmp_path):
        assert allowed_tools() == set()
        assert denial('terminal')


def test_actual_platform_config_and_memory_are_profile_scoped(tmp_path):
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from hermes_constants import get_hermes_home
    profiles = [tmp_path / name for name in ('alpha', 'beta')]
    for home in profiles:
        home.mkdir()
        (home / 'config.yaml').write_text(yaml.safe_dump({
            'platform_toolsets': {'api_server': ['web', 'memory']},
            'agent': {'managed_allowed_tools': ['web_search', 'web_extract', 'memory']}}))
        (home / 'memories').mkdir()
        (home / 'memories' / 'MEMORY.md').write_text(home.name)
    for home in [*profiles, profiles[0]]:
        with _profile_runtime_scope(home):
            selected = _get_platform_tools(load_config(), 'api_server', include_default_mcp_servers=False)
            assert {'web', 'memory'} <= selected
            # The fork adds escalation to every platform; the managed ceiling removes it.
            assert _select_tool_names(list(selected), None, True) == {'web_search', 'web_extract', 'memory'}
            assert (get_hermes_home() / 'memories' / 'MEMORY.md').read_text() == home.name
