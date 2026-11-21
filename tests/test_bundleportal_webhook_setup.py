import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import setup_bundleportal_webhook as setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.output = Path(self.folder.name) / '.env.bundleportal-webhook'
        self.stream = io.StringIO()
        env = patch.dict(os.environ, {'BUNDLE_PORTAL_KEY': 'test-key', 'BUNDLEPORTAL_API_KEY': '',
            'BUNDLE_PORTAL_WEBHOOK_SECRET': '', 'BUNDLEPORTAL_WEBHOOK_SECRET': ''})
        env.start()
        self.addCleanup(env.stop)

    def test_registers_v2_target_and_saves_secret_without_printing_it(self):
        with (patch.object(setup, 'receiver_info', return_value={'configured': False}), patch.object(setup,
                'api_call', side_effect=[{'webhook_url': None}, {'webhook_url': setup.DEFAULT_URL, 'webhook_secret': 'whsec_test'}]) as api,
                contextlib.redirect_stdout(self.stream)):
            self.assertEqual(setup.main(['--output', str(self.output)]), 0)
        self.assertEqual(api.call_args_list[0].args, ('test-key', 'get_webhook'))
        self.assertEqual(api.call_args_list[1].args, ('test-key', 'set_webhook'))
        self.assertEqual(api.call_args_list[1].kwargs['webhook_url'], 'https://sirhans.com/webhooks/bundleportal')
        self.assertIn('BUNDLE_PORTAL_WEBHOOK_SECRET=whsec_test', self.output.read_text())
        self.assertNotIn('whsec_test', self.stream.getvalue())
        self.assertNotIn('test-key', self.output.read_text())

    def test_existing_registration_is_not_silently_rotated(self):
        with patch.object(setup, 'receiver_info', return_value={'configured': True}), patch.object(setup,
                'api_call', return_value={'webhook_url': setup.DEFAULT_URL}) as api, contextlib.redirect_stdout(self.stream):
            self.assertEqual(setup.main(['--output', str(self.output)]), 0)
        api.assert_called_once_with('test-key', 'get_webhook')
        self.assertFalse(self.output.exists())

    def test_explicit_rotation_archives_previous_secret(self):
        self.output.write_text('BUNDLE_PORTAL_WEBHOOK_SECRET=whsec_old\n')
        with patch.object(setup, 'receiver_info', return_value={'configured': True}), patch.object(setup,
                'api_call', side_effect=[{'webhook_url': setup.DEFAULT_URL}, {'webhook_secret': 'whsec_new'}]), contextlib.redirect_stdout(self.stream):
            self.assertEqual(setup.main(['--rotate', '--output', str(self.output)]), 0)
        self.assertIn('whsec_new', self.output.read_text())
        backups = list(self.output.parent.glob('.env.bundleportal-webhook.previous-*'))
        self.assertEqual(len(backups), 1)
        self.assertIn('whsec_old', backups[0].read_text())

    def test_signed_configuration_probe_uses_saved_secret_without_api_registration(self):
        self.output.write_text('BUNDLE_PORTAL_WEBHOOK_SECRET=whsec_test\n')
        with patch.object(setup, 'http_json', return_value=(200, {'success': True, 'test': True})) as http, contextlib.redirect_stdout(self.stream):
            self.assertEqual(setup.main(['--test', '--output', str(self.output)]), 0)
        self.assertEqual(http.call_args.args[1], {'event': 'webhook.test'})
        self.assertTrue(http.call_args.args[2]['X-BundlePortal-Signature'].startswith('sha256='))

    def test_missing_receiver_prevents_registration(self):
        with patch.object(setup, 'receiver_info', side_effect=RuntimeError('Deploy receiver first')), patch.object(setup, 'api_call') as api, contextlib.redirect_stderr(self.stream):
            self.assertEqual(setup.main(['--output', str(self.output)]), 1)
        api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
