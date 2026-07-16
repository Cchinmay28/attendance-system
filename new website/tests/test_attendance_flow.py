import importlib
import os
import unittest
from unittest.mock import patch

import app as app_module


class AttendanceFlowTests(unittest.TestCase):
    def setUp(self):
        app_module.DEMO_MODE = False
        app_module.PUBLIC_DEPLOYMENT = False
        app_module.ALLOW_LOOPBACK = False
        app_module.OFFICE_ALLOWED_IPS = ['192.168.0.0/24']
        app_module.TRUSTED_PROXY_IPS = []
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
            response = client.get('/login', environ_overrides={'REMOTE_ADDR': '8.8.8.8'})

        self.assertEqual(response.status_code, 200)
        self.assertIn('Office Attendance - Login', response.get_data(as_text=True))
        self.assertFalse(reloaded.DEMO_MODE)
        self.assertIn('192.168.0.0/24', reloaded.OFFICE_ALLOWED_IPS)

    def test_access_denied_page_renders_content(self):
        client = self.client
        response = client.get('/access-denied')

        self.assertEqual(response.status_code, 403)
        self.assertIn('Access Denied', response.get_data(as_text=True))

    def test_untrusted_xff_header_does_not_bypass_access_control(self):
        app_module.write_csv(app_module.DENIED_CSV, app_module.DENIED_HEADERS, [])

        response = self.client.post('/login', data={
            'login_type': 'employee',
            'employee_id': '101',
            'employee_password': 'pass101',
        }, environ_overrides={
            'REMOTE_ADDR': '8.8.8.8',
            'HTTP_X_FORWARDED_FOR': '127.0.0.1'
        }, follow_redirects=True)

        self.assertEqual(response.status_code, 403)
        self.assertIn('Access Denied', response.get_data(as_text=True))
        denied = app_module.read_csv(app_module.DENIED_CSV)
        self.assertEqual(denied[0]['detected_ip'], '8.8.8.8')

    def test_non_office_ip_can_see_login_page_before_authentication(self):
        app_module.write_csv(app_module.DENIED_CSV, app_module.DENIED_HEADERS, [])

        response = self.client.get('/login', environ_overrides={'REMOTE_ADDR': '8.8.8.8'})

        self.assertEqual(response.status_code, 200)
        self.assertIn('Office Attendance - Login', response.get_data(as_text=True))
        self.assertEqual(app_module.read_csv(app_module.DENIED_CSV), [])

    def test_valid_login_from_non_office_ip_is_denied_logged_and_session_cleared(self):
        app_module.write_csv(app_module.DENIED_CSV, app_module.DENIED_HEADERS, [])
        app_module.write_csv(app_module.LOGIN_LOGS_CSV, app_module.LOGIN_LOG_HEADERS, [])

        response = self.client.post('/login', data={
            'login_type': 'employee',
            'employee_id': '101',
            'employee_password': 'pass101',
        }, environ_overrides={'REMOTE_ADDR': '8.8.8.8'}, follow_redirects=True)

        self.assertEqual(response.status_code, 403)
        self.assertIn('Access Denied', response.get_data(as_text=True))
        denied = app_module.read_csv(app_module.DENIED_CSV)
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]['employee_id'], '101')
        self.assertEqual(denied[0]['action'], 'login')
        self.assertEqual(denied[0]['detected_ip'], '8.8.8.8')
        login_logs = app_module.read_csv(app_module.LOGIN_LOGS_CSV)
        self.assertEqual(login_logs[0]['status'], 'denied-network')
        with self.client.session_transaction() as sess:
            self.assertNotIn('role', sess)

    def test_authenticated_session_from_non_office_ip_cannot_access_dashboard(self):
        app_module.write_csv(app_module.DENIED_CSV, app_module.DENIED_HEADERS, [])
        with self.client.session_transaction() as sess:
            sess.update({
                'role': 'employee',
                'employee_id': '101',
                'employee_name': 'John Smith',
                'department': 'Recruiting',
                'office': 'Head Office',
            })

        response = self.client.get('/dashboard', environ_overrides={'REMOTE_ADDR': '8.8.8.8'}, follow_redirects=True)

        self.assertEqual(response.status_code, 403)
        self.assertIn('Access Denied', response.get_data(as_text=True))
        denied = app_module.read_csv(app_module.DENIED_CSV)
        self.assertEqual(denied[0]['employee_id'], '101')
        self.assertEqual(denied[0]['action'], 'dashboard')
        with self.client.session_transaction() as sess:
            self.assertNotIn('role', sess)

    def test_localhost_access_allowed_when_host_has_allowed_lan_ip(self):
        with patch('app.get_local_ip_addresses', return_value={'192.168.0.51'}):
            response = self.client.get('/login', environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('Office Attendance - Login', response.get_data(as_text=True))

    def test_localhost_access_denied_when_host_has_no_allowed_lan_ip(self):
        with patch('app.get_local_ip_addresses', return_value={'192.168.1.100'}):
            response = self.client.get('/login', environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('Office Attendance - Login', response.get_data(as_text=True))

    def test_localhost_access_allowed_with_env_flag(self):
        original_allow = app_module.ALLOW_LOOPBACK
        app_module.ALLOW_LOOPBACK = True
        try:
            response = self.client.get('/login', environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('Office Attendance - Login', response.get_data(as_text=True))
        finally:
            app_module.ALLOW_LOOPBACK = original_allow

    def test_successful_employee_login_logs_detected_ip(self):
        app_module.write_csv(app_module.LOGIN_LOGS_CSV, app_module.LOGIN_LOG_HEADERS, [])

        response = self.client.post('/login', data={
            'login_type': 'employee',
            'employee_id': '101',
            'employee_password': 'pass101',
        }, environ_overrides={'REMOTE_ADDR': '192.168.0.10'})

        self.assertEqual(response.status_code, 302)
        login_logs = app_module.read_csv(app_module.LOGIN_LOGS_CSV)
        self.assertEqual(len(login_logs), 1)
        self.assertEqual(login_logs[0]['user_id'], '101')
        self.assertEqual(login_logs[0]['role'], 'employee')
        self.assertEqual(login_logs[0]['detected_ip'], '192.168.0.10')

    def test_localhost_denied_in_public_deployment(self):
        with patch.dict(os.environ, {'PUBLIC_DEPLOYMENT': 'true'}, clear=False):
            reloaded = importlib.reload(app_module)
            client = reloaded.app.test_client()
            response = client.get('/login', environ_overrides={'REMOTE_ADDR': '127.0.0.1'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('Office Attendance - Login', response.get_data(as_text=True))

    def test_break_flow_updates_attendance_record(self):
        with self.client.session_transaction() as sess:
            sess.update({
                'role': 'employee',
                'employee_id': '101',
                'employee_name': 'John Smith',
                'department': 'Recruiting',
                'office': 'Head Office',
            })

        app_module.write_csv(app_module.ATTENDANCE_CSV, app_module.ATT_HEADERS, [])
        app_module.write_csv(app_module.BREAKS_CSV, app_module.BREAK_HEADERS, [])
        clock_in_response = self.client.post('/clock-in', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})
        self.assertTrue(clock_in_response.get_json()['success'])

        start_break_response = self.client.post('/break/start', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})
        self.assertTrue(start_break_response.get_json()['success'])

        end_break_response = self.client.post('/break/end', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})
        self.assertTrue(end_break_response.get_json()['success'])

        records = app_module.read_csv(app_module.ATTENDANCE_CSV)
        breaks = app_module.read_csv(app_module.BREAKS_CSV)
        self.assertEqual(records[0]['break_count'], '1')
        self.assertNotEqual(records[0]['total_break_time'], '')
        self.assertEqual(records[0]['on_break'], 'No')
        self.assertEqual(breaks[0]['break_start_ip'], '192.168.0.10')
        self.assertEqual(breaks[0]['break_end_ip'], '192.168.0.10')

        attendance_response = self.client.get('/attendance', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})
        attendance_payload = attendance_response.get_json()
        self.assertEqual(attendance_payload['break_count'], '1')
        self.assertEqual(len(attendance_payload['breaks']), 1)
        self.assertIn('net_working_time', attendance_payload)

    def test_multiple_breaks_are_stored_for_one_attendance_record(self):
        with self.client.session_transaction() as sess:
            sess.update({
                'role': 'employee',
                'employee_id': '101',
                'employee_name': 'John Smith',
                'department': 'Recruiting',
                'office': 'Head Office',
            })

        app_module.write_csv(app_module.ATTENDANCE_CSV, app_module.ATT_HEADERS, [])
        app_module.write_csv(app_module.BREAKS_CSV, app_module.BREAK_HEADERS, [])
        self.client.post('/clock-in', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})
        self.client.post('/break/start', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})
        self.client.post('/break/end', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})
        self.client.post('/break/start', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})
        self.client.post('/break/end', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})

        payload = self.client.get('/attendance', environ_overrides={'REMOTE_ADDR': '192.168.0.10'}).get_json()
        self.assertEqual(payload['break_count'], '2')
        self.assertEqual(len(payload['breaks']), 2)

    def test_clock_out_is_blocked_while_break_is_active(self):
        with self.client.session_transaction() as sess:
            sess.update({
                'role': 'employee',
                'employee_id': '101',
                'employee_name': 'John Smith',
                'department': 'Recruiting',
                'office': 'Head Office',
            })

        app_module.write_csv(app_module.ATTENDANCE_CSV, app_module.ATT_HEADERS, [])
        app_module.write_csv(app_module.BREAKS_CSV, app_module.BREAK_HEADERS, [])
        self.client.post('/clock-in', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})
        self.client.post('/break/start', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})

        response = self.client.post('/clock-out', environ_overrides={'REMOTE_ADDR': '192.168.0.10'})
        payload = response.get_json()

        self.assertFalse(payload['success'])
        self.assertIn('break', payload['message'].lower())


if __name__ == '__main__':
    unittest.main()
