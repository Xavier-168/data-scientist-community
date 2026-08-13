import importlib.util
import json
import pathlib
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


MODULE_PATH = pathlib.Path(__file__).with_name("package_identity.py")
SPEC = importlib.util.spec_from_file_location("package_identity_module", MODULE_PATH)
package_identity = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(package_identity)


class PackageIdentityTests(unittest.TestCase):
    def test_signed_manifest_verifies_and_binds_license(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            (root / "runtime").mkdir(parents=True, exist_ok=True)
            (root / "scripts").mkdir(parents=True, exist_ok=True)

            private_key = ed25519.Ed25519PrivateKey.generate()
            public_pem = private_key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("utf-8")
            (root / "scripts" / "package_public_keys.json").write_text(
                json.dumps(
                    {
                        "active_key_id": "pkg-test",
                        "keys": [{"key_id": "pkg-test", "public_key_pem": public_pem}],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            signed_manifest = package_identity.sign_package_manifest(
                {
                    "key_id": "pkg-test",
                    "package_id": "pkg-001",
                    "customer_name": "测试客户",
                    "license_key_sha256": package_identity.license_fingerprint("YRG-TEST-1111"),
                },
                private_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode("utf-8"),
            )
            package_identity.write_signed_package_manifest(str(root), signed_manifest)

            status = package_identity.verify_package_manifest(str(root))
            self.assertTrue(status["ok"])

            allowed, error = package_identity.package_license_allowed(str(root), "YRG-TEST-1111")
            self.assertTrue(allowed)
            self.assertEqual(error, "")

            denied, error = package_identity.package_license_allowed(str(root), "YRG-OTHER-2222")
            self.assertFalse(denied)
            self.assertEqual(error, "license_not_allowed_for_package")


if __name__ == "__main__":
    unittest.main()
