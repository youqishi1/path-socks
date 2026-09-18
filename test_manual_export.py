"""Presentation-only export must not change stored state or expose server private key."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
import reality


class ManualExportTests(unittest.TestCase):
    def test_default_link_and_optional_helper(self):
        state={'host':'8.8.8.8','port':26443,'sni':'example.com','public':'P'*43,'private':'NEVER-EXPORT-PRIVATE', 'sid':'0123456789abcdef','users':[{'label':'用户1','id':'11111111-1111-4111-8111-111111111111'}]}
        before=copy.deepcopy(state)
        for backup in (False,True):
            output=io.StringIO()
            with patch.object(reality,'prompt',return_value='1'),contextlib.redirect_stdout(output):
                reality.show(state,backup=backup)
            value=output.getvalue()
            self.assertNotIn(state['private'],value)
            self.assertEqual(state,before)
            if backup:
                self.assertIn('SBB1.',value)
                self.assertNotIn('vless://',value)
            else:
                self.assertNotIn('SBB1.',value)
                uri=next(line for line in value.splitlines() if line.startswith('vless://'))
                parsed=urlsplit(uri);query=parse_qs(parsed.query)
                self.assertEqual(parsed.hostname,state['host'])
                self.assertEqual(parsed.port,state['port'])
                self.assertEqual(parsed.username,state['users'][0]['id'])
                self.assertEqual(query['pbk'],[state['public']])
                self.assertEqual(query['flow'],['xtls-rprx-vision'])
                self.assertIn('前置代理别名',value)


if __name__=='__main__':unittest.main()
