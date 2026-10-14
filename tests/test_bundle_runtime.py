import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
from test_bundle_install import BundleInstallTests


class BundleRuntimeTests(BundleInstallTests):
    def selected(self):
        from telegram_search_mcp import bundle_runtime
        packet = self.fixture()
        result = self.install(packet)
        selection = bundle_runtime.Selection(Path(result['release_root']),packet[3])
        patcher = mock.patch.object(bundle_runtime,'_SELECTION',selection)
        patcher.start(); self.addCleanup(patcher.stop)
        return selection

    def test_launch_agent_uses_exact_receipt_bound_isolated_form(self):
        from telegram_search_mcp import launch_agent
        selection = self.selected()
        payload = launch_agent.launch_agent_payload(python_executable=Path(sys.executable))
        expected = [str(selection.root/'python/bin/python3'),'-I','-S','-B',str(selection.root/'launcher.py'),'broker']
        self.assertEqual(payload['ProgramArguments'],expected)
        self.assertEqual(payload['EnvironmentVariables'],{})
        self.assertTrue(launch_agent._managed_payload(payload))
        for index, value in [(1,'-m'),(4,'/foreign/launcher.py'),(5,'setup')]:
            altered = {**payload,'ProgramArguments':list(expected)}
            altered['ProgramArguments'][index] = value
            self.assertFalse(launch_agent._managed_payload(altered))

    def test_invalid_receipt_cannot_select_native_or_broker(self):
        from telegram_search_mcp import bundle_runtime
        selection = self.selected()
        (self.runtime/'current.json').write_text('{}')
        with self.assertRaises(bundle_runtime.BundleError): bundle_runtime.broker_arguments()
        with mock.patch('telegram_search_mcp.native_runtime.validate_native') as native:
            with self.assertRaises(bundle_runtime.BundleError): bundle_runtime.selected_native()
            native.assert_not_called()

    def test_arbitrary_environment_does_not_select_a_bundled_library(self):
        from telegram_search_mcp import bundle_runtime
        with mock.patch.object(bundle_runtime,'_SELECTION',None), mock.patch.dict('os.environ',
                {'TELEGRAM_SEARCH_RELEASE_ROOT':'/foreign','DYLD_LIBRARY_PATH':'/foreign','TDLIB_LIBRARY':'/foreign'}):
            self.assertIsNone(bundle_runtime.selected_native())

    def test_developer_python_cannot_activate_bundled_context(self):
        from telegram_search_mcp import bundle_runtime
        selection = self.selected()
        with self.assertRaises(bundle_runtime.BundleError): bundle_runtime.activate(selection.root,selection.index_sha256)

    def test_matching_pointer_cannot_mask_registration_or_paired_skill_drift(self):
        from telegram_search_mcp import bundle_runtime
        for kind in ['registration','skill','receipt_home']:
            with self.subTest(kind=kind):
                self.setUp();selection=self.selected()
                if kind=='registration':
                    p=self.codex/'config.toml';p.write_text('[mcp_servers.telegram_search]\ncommand="/foreign"\n')
                elif kind=='skill':(self.skills/'telegram-setup/SKILL.md').write_bytes(b'stale skill')
                else:
                    p=self.runtime/'receipt.json';value=json.loads(p.read_text());value['user_home']='/foreign';p.write_text(json.dumps(value))
                with self.assertRaises(bundle_runtime.BundleError):selection.check()

    def test_bundled_manifest_names_actual_native_pin_without_loading_provider(self):
        from telegram_search_mcp import server
        from telegram_search_mcp.config import RuntimePolicy
        self.selected()
        mcp = server.build_server(policy=RuntimePolicy())
        tool = mcp._tool_manager.get_tool('_manifest')
        result = tool.fn(check_broker=False)
        self.assertIn('bundled',result['runtime_pin'])
        self.assertNotIn('Homebrew',result['runtime_pin'])

    def test_launcher_help_is_credential_free_without_loading_product(self):
        path = Path(__file__).parents[1]/'packaging/launcher.py'
        self.assertTrue(path.is_file(),'missing isolated standalone launcher')
        spec = importlib.util.spec_from_file_location('test_standalone_launcher',path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(module.main(['--help']),0)
        self.assertIn('setup',output.getvalue())
        self.assertNotIn('secret',output.getvalue().lower())

    def test_mcp_dispatch_clears_launcher_action_before_server_argument_parser(self):
        path = Path(__file__).parents[1]/'packaging/launcher.py'
        spec = importlib.util.spec_from_file_location('mcp_dispatch_test',path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.assertTrue(hasattr(module,'dispatch_mcp'),'missing isolated MCP argv dispatch')
        def consumer():
            self.assertEqual(sys.argv[1:],[])
            return 0
        with mock.patch('telegram_search_mcp.server.main',side_effect=consumer), mock.patch.object(sys,'argv',['launcher.py','mcp']):
            self.assertEqual(module.dispatch_mcp(),0)
