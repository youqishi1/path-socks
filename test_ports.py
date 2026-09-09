import socket
import unittest
from unittest.mock import patch

import port


class PortTests(unittest.TestCase):
    def test_occupied_port_is_skipped(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("0.0.0.0", port.choose(25443)))
            listener.listen()
            occupied = listener.getsockname()[1]
            self.assertFalse(port.available(occupied))
            selected = port.choose(occupied)
            self.assertNotEqual(selected, occupied)
            self.assertTrue(20000 <= selected < 30000)
            with self.assertRaises(ValueError):
                port.choose(occupied, explicit=True)

    def test_invalid_port(self):
        for number in (80, 443, 10239, 65536):
            with self.assertRaises(ValueError):
                port.choose(number)

    def test_owned_port_reused(self):
        with patch.object(port, "available", return_value=False), patch.object(port, "owned_listener", return_value=True):
            self.assertEqual(port.choose(25443, pid=100, explicit=True), 25443)

    def test_free_port(self):
        with patch.object(port, "available", return_value=True):
            self.assertEqual(port.choose(25443), 25443)


if __name__ == "__main__":
    unittest.main()
