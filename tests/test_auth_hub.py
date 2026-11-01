"""Offline tests for account security primitives and encryption."""
import os, tempfile, unittest
from pathlib import Path
_TMP = tempfile.TemporaryDirectory(prefix="genesis-auth-tests-")
os.environ["GENESIS_DB"] = str(Path(_TMP.name) / "auth.db")
os.environ["GENESIS_TOKEN_ENCRYPTION_KEY"] = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
import auth_hub

class AuthHubTests(unittest.TestCase):
    def test_password_hash_and_verify(self):
        encoded = auth_hub._password_hash("a-long-password-123")
        self.assertTrue(auth_hub._verify_password("a-long-password-123", encoded))
        self.assertFalse(auth_hub._verify_password("wrong-password", encoded))
    def test_oauth_token_encryption_roundtrip(self):
        payload = {"access_token":"test-access","refresh_token":"test-refresh","expires_in":3600}
        ciphertext, nonce = auth_hub._encrypt(payload)
        self.assertNotIn("test-access", ciphertext)
        self.assertEqual(auth_hub._decrypt(ciphertext, nonce), payload)
    def test_token_encryption_rejects_tampering(self):
        ciphertext, nonce = auth_hub._encrypt({"access_token":"secret"})
        altered = ciphertext[:-3] + ("AAA" if ciphertext[-3:] != "AAA" else "BBB")
        with self.assertRaises(Exception):
            auth_hub._decrypt(altered, nonce)
if __name__ == "__main__":
    unittest.main()
