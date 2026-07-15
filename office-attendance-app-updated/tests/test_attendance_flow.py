import importlib
import os
import unittest
from unittest.mock import patch

import app as app_module


class AttendanceFlowTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_clock_in_returns_json_error_when_storage_fails(self):
        with self.client.session_transaction() as sess:
            sess.update({
                'role': 'employee',
                'employee_id': '101',
                'employee_name': 'John Smith',
                'department': 'Recruiting',
                'office': 'Head Office',
            })

        app_module.write_csv(app_module.ATTENDANCE_CSV, app_module.ATT_HEADERS, [])

        with patch('app.write_csv', side_effect=PermissionError('simulated write failure')):
            response = self.client.post('/clock-in', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})

        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        payload = response.get_json()
        self.assertFalse(payload['success'])
        self.assertIn('could not be saved', payload['message'].lower())

    def test_vercel_environment_keeps_strict_office_access(self):
        with patch.dict(os.environ, {'VERCEL': '1', 'OFFICE_ALLOWED_IPS': '192.168.0.0/24'}, clear=True):
            reloaded = importlib.reload(app_module)
            client = reloaded.app.test_client()
            response = client.get('/login', environ_overrides={'REMOTE_ADDR': '8.8.8.8'}, follow_redirects=True)

        self.assertEqual(response.status_code, 403)
        self.assertIn('Access Denied', response.get_data(as_text=True))
        self.assertFalse(reloaded.DEMO_MODE)
        self.assertIn('192.168.0.0/24', reloaded.OFFICE_ALLOWED_IPS)

    def test_access_denied_page_renders_content(self):
        client = self.client
        response = client.get('/access-denied')

        self.assertEqual(response.status_code, 403)
        self.assertIn('Access Denied', response.get_data(as_text=True))

    def test_untrusted_xff_header_does_not_bypass_access_control(self):
        response = self.client.get('/login', environ_overrides={
            'REMOTE_ADDR': '8.8.8.8',
            'HTTP_X_FORWARDED_FOR': '127.0.0.1'
        }, follow_redirects=True)

        self.assertEqual(response.status_code, 403)
        self.assertIn('Access Denied', response.get_data(as_text=True))

    def test_non_office_ip_sees_access_denied(self):
        response = self.client.get('/login', environ_overrides={'REMOTE_ADDR': '8.8.8.8'}, follow_redirects=True)

        self.assertEqual(response.status_code, 403)
        self.assertIn('Access Denied', response.get_data(as_text=True))

    def test_localhost_access_allowed_when_host_has_allowed_lan_ip(self):
        with patch('app.get_local_ip_addresses', return_value={'192.168.0.51'}):
            response = self.client.get('/login', environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('Office Attendance — Login', response.get_data(as_text=True))

    def test_localhost_access_denied_when_host_has_no_allowed_lan_ip(self):
        with patch('app.get_local_ip_addresses', return_value={'192.168.1.100'}):
            response = self.client.get('/login', environ_overrides={'REMOTE_ADDR': '127.0.0.1'}, follow_redirects=True)
            self.assertEqual(response.status_code, 403)
            self.assertIn('Access Denied', response.get_data(as_text=True))

    def test_localhost_access_allowed_with_env_flag(self):
        original_allow = app_module.ALLOW_LOOPBACK
        app_module.ALLOW_LOOPBACK = True
        try:
            response = self.client.get('/login', environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('Office Attendance — Login', response.get_data(as_text=True))
        finally:
            app_module.ALLOW_LOOPBACK = original_allow

    def test_localhost_denied_in_public_deployment(self):
        with patch.dict(os.environ, {'PUBLIC_DEPLOYMENT': 'true'}, clear=False):
            reloaded = importlib.reload(app_module)
            client = reloaded.app.test_client()
            response = client.get('/login', environ_overrides={'REMOTE_ADDR': '127.0.0.1'}, follow_redirects=True)
            self.assertEqual(response.status_code, 403)
            self.assertIn('Access Denied', response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
