use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use ed25519_dalek::{pkcs8::DecodePublicKey, Signature, Verifier, VerifyingKey};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use thiserror::Error;

const MAX_RUNTIME_VERSION_LENGTH: usize = 128;
const MAX_SIGNED_MANIFEST_BYTES: usize = 2 * 1024 * 1024;
const MAX_TRUSTED_KEY_BUNDLE_BYTES: usize = 1024 * 1024;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeDescriptor {
    pub version: String,
    pub archive: String,
    pub sha256: String,
    pub tree_sha256: String,
    pub size_bytes: u64,
    pub required_files: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeSet {
    pub core: RuntimeDescriptor,
    pub collector: RuntimeDescriptor,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct PackageManifest {
    pub build_version: String,
    pub key_id: String,
    pub package_id: String,
    #[serde(default)]
    pub arch: String,
    #[serde(default)]
    pub supported_architectures: Vec<String>,
    pub runtimes: RuntimeSet,
}

#[derive(Clone)]
pub struct VerifiedPackageManifest {
    manifest: PackageManifest,
    signed_bytes: Vec<u8>,
    key_bundle_bytes: Vec<u8>,
}

impl std::fmt::Debug for VerifiedPackageManifest {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("VerifiedPackageManifest")
            .field("package_id", &self.manifest.package_id)
            .field("build_version", &self.manifest.build_version)
            .field("key_id", &self.manifest.key_id)
            .field("signed_bytes_len", &self.signed_bytes.len())
            .field("key_bundle_bytes_len", &self.key_bundle_bytes.len())
            .finish()
    }
}

impl VerifiedPackageManifest {
    pub fn from_signed(
        manifest_bytes: &[u8],
        key_bundle_bytes: &[u8],
    ) -> Result<Self, ManifestError> {
        let manifest = verify_signed_manifest_core(manifest_bytes, key_bundle_bytes)?;
        Ok(Self {
            manifest,
            signed_bytes: manifest_bytes.to_vec(),
            key_bundle_bytes: key_bundle_bytes.to_vec(),
        })
    }

    pub fn manifest(&self) -> &PackageManifest {
        &self.manifest
    }

    pub fn signed_bytes(&self) -> &[u8] {
        &self.signed_bytes
    }

    pub fn key_bundle_bytes(&self) -> &[u8] {
        &self.key_bundle_bytes
    }

    pub fn reverify(&self, signed_bytes: &[u8]) -> Result<Self, ManifestError> {
        Self::from_signed(signed_bytes, &self.key_bundle_bytes)
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SignedEnvelope {
    payload: Value,
    signature: String,
}

#[derive(Deserialize)]
struct TrustedKeyBundle {
    keys: Vec<TrustedKey>,
}

#[derive(Deserialize)]
struct TrustedKey {
    key_id: String,
    public_key_pem: String,
}

#[derive(Debug, Error)]
pub enum ManifestError {
    #[error("manifest_input_too_large: {0}")]
    InputTooLarge(&'static str),
    #[error("manifest_json_invalid: {0}")]
    Json(#[from] serde_json::Error),
    #[error("manifest_signature_encoding_invalid")]
    SignatureEncoding,
    #[error("manifest_key_untrusted")]
    KeyUntrusted,
    #[error("manifest_signature_invalid")]
    SignatureInvalid,
    #[error("manifest_schema_invalid: {0}")]
    Schema(String),
}

fn canonical_json(value: &Value, output: &mut String) -> Result<(), serde_json::Error> {
    match value {
        Value::Null => output.push_str("null"),
        Value::Bool(value) => output.push_str(if *value { "true" } else { "false" }),
        Value::Number(value) => output.push_str(&value.to_string()),
        Value::String(value) => output.push_str(&serde_json::to_string(value)?),
        Value::Array(values) => {
            output.push('[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                canonical_json(value, output)?;
            }
            output.push(']');
        }
        Value::Object(values) => {
            output.push('{');
            let mut keys: Vec<_> = values.keys().collect();
            keys.sort_unstable();
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                output.push_str(&serde_json::to_string(key)?);
                output.push(':');
                canonical_json(&values[key], output)?;
            }
            output.push('}');
        }
    }
    Ok(())
}

fn valid_runtime_version(value: &str) -> bool {
    valid_identifier(value)
}

fn valid_identifier(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= MAX_RUNTIME_VERSION_LENGTH
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(byte))
        && !value.contains(".tmp-")
}

fn canonical_numbers_supported(value: &Value) -> bool {
    match value {
        Value::Number(number) => number.is_i64() || number.is_u64(),
        Value::Array(values) => values.iter().all(canonical_numbers_supported),
        Value::Object(values) => values.values().all(canonical_numbers_supported),
        _ => true,
    }
}

fn canonical_relative_posix(value: &str) -> bool {
    !value.is_empty()
        && !value.contains(['\\', '\0'])
        && value
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != "..")
}

fn sorted_unique(values: &[String]) -> bool {
    values
        .windows(2)
        .all(|window| window[0].as_bytes() < window[1].as_bytes())
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn validate_descriptor(name: &str, descriptor: &RuntimeDescriptor) -> Result<(), ManifestError> {
    if !valid_runtime_version(&descriptor.version) {
        return Err(ManifestError::Schema(format!("{name}.version")));
    }
    if descriptor.archive != format!("{name}-runtime.tar.zst") {
        return Err(ManifestError::Schema(format!("{name}.archive")));
    }
    if !valid_sha256(&descriptor.sha256) {
        return Err(ManifestError::Schema(format!("{name}.sha256")));
    }
    if !valid_sha256(&descriptor.tree_sha256) {
        return Err(ManifestError::Schema(format!("{name}.tree_sha256")));
    }
    if descriptor.size_bytes == 0 {
        return Err(ManifestError::Schema(format!("{name}.size_bytes")));
    }
    if descriptor.required_files.is_empty()
        || !descriptor
            .required_files
            .iter()
            .all(|item| canonical_relative_posix(item))
        || !sorted_unique(&descriptor.required_files)
    {
        return Err(ManifestError::Schema(format!("{name}.required_files")));
    }
    Ok(())
}

pub fn verify_signed_manifest(
    manifest_bytes: &[u8],
    key_bundle_bytes: &[u8],
) -> Result<PackageManifest, ManifestError> {
    verify_signed_manifest_core(manifest_bytes, key_bundle_bytes)
}

fn verify_signed_manifest_core(
    manifest_bytes: &[u8],
    key_bundle_bytes: &[u8],
) -> Result<PackageManifest, ManifestError> {
    if manifest_bytes.len() > MAX_SIGNED_MANIFEST_BYTES {
        return Err(ManifestError::InputTooLarge("manifest"));
    }
    if key_bundle_bytes.len() > MAX_TRUSTED_KEY_BUNDLE_BYTES {
        return Err(ManifestError::InputTooLarge("key_bundle"));
    }
    let envelope: SignedEnvelope = serde_json::from_slice(manifest_bytes)?;
    let key_id = envelope
        .payload
        .get("key_id")
        .and_then(Value::as_str)
        .filter(|value| valid_identifier(value))
        .ok_or_else(|| ManifestError::Schema("key_id".into()))?;
    if !canonical_numbers_supported(&envelope.payload) {
        return Err(ManifestError::Schema(
            "non-integer JSON number is not supported".into(),
        ));
    }
    let bundle: TrustedKeyBundle = serde_json::from_slice(key_bundle_bytes)?;
    let matching: Vec<_> = bundle
        .keys
        .iter()
        .filter(|item| item.key_id == key_id)
        .collect();
    if matching.len() != 1 {
        return Err(ManifestError::KeyUntrusted);
    }
    let verifying_key = VerifyingKey::from_public_key_pem(&matching[0].public_key_pem)
        .map_err(|_| ManifestError::KeyUntrusted)?;
    let signature_bytes = URL_SAFE_NO_PAD
        .decode(envelope.signature)
        .map_err(|_| ManifestError::SignatureEncoding)?;
    let signature =
        Signature::from_slice(&signature_bytes).map_err(|_| ManifestError::SignatureEncoding)?;
    let mut canonical = String::new();
    canonical_json(&envelope.payload, &mut canonical)?;
    verifying_key
        .verify(canonical.as_bytes(), &signature)
        .map_err(|_| ManifestError::SignatureInvalid)?;

    let manifest: PackageManifest = serde_json::from_value(envelope.payload)
        .map_err(|error| ManifestError::Schema(error.to_string()))?;
    let arm64 = manifest.arch == "arm64"
        || manifest
            .supported_architectures
            .iter()
            .any(|architecture| architecture == "arm64");
    if !valid_identifier(&manifest.key_id)
        || !valid_identifier(&manifest.package_id)
        || !valid_identifier(&manifest.build_version)
        || !arm64
    {
        return Err(ManifestError::Schema("package identity or arm64".into()));
    }
    validate_descriptor("core", &manifest.runtimes.core)?;
    validate_descriptor("collector", &manifest.runtimes.collector)?;
    Ok(manifest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{
        pkcs8::{spki::der::pem::LineEnding, EncodePublicKey},
        Signer, SigningKey,
    };
    use serde_json::{json, Value};

    fn payload() -> Value {
        json!({
            "arch": "arm64",
            "build_version": "20260711",
            "customer_name": "测试客户",
            "features": ["采集", "analytics"],
            "key_id": "test-key",
            "package_id": "data-scientist-community-mac-arm64",
            "runtimes": {
                "core": {
                    "version": "core-20260711.1",
                    "archive": "core-runtime.tar.zst",
                    "sha256": "a".repeat(64),
                    "tree_sha256": "c".repeat(64),
                    "size_bytes": 100,
                    "required_files": ["frontend-compat/progress.html", "scripts/_run.py"]
                },
                "collector": {
                    "version": "collector_20260711",
                    "archive": "collector-runtime.tar.zst",
                    "sha256": "b".repeat(64),
                    "tree_sha256": "d".repeat(64),
                    "size_bytes": 200,
                    "required_files": ["node_modules/playwright/package.json", "scripts/douyin_export.mjs"]
                }
            }
        })
    }

    fn signing_key() -> SigningKey {
        SigningKey::from_bytes(&[7_u8; 32])
    }

    fn key_bundle(signing: &SigningKey) -> Vec<u8> {
        let pem = signing
            .verifying_key()
            .to_public_key_pem(LineEnding::LF)
            .unwrap();
        serde_json::to_vec(&json!({
            "active_key_id": "test-key",
            "keys": [{"key_id": "test-key", "public_key_pem": pem}]
        }))
        .unwrap()
    }

    fn signed(payload: Value, signing: &SigningKey) -> Vec<u8> {
        let mut canonical = String::new();
        canonical_json(&payload, &mut canonical).unwrap();
        let signature = URL_SAFE_NO_PAD.encode(signing.sign(canonical.as_bytes()).to_bytes());
        serde_json::to_vec(&json!({"payload": payload, "signature": signature})).unwrap()
    }

    fn verify(payload: Value) -> Result<PackageManifest, ManifestError> {
        let signing = signing_key();
        verify_signed_manifest(&signed(payload, &signing), &key_bundle(&signing))
    }

    #[test]
    fn canonical_json_matches_python_package_identity_bytes() {
        let mut canonical = String::new();
        canonical_json(&payload(), &mut canonical).unwrap();
        let expected = concat!(
            "{\"arch\":\"arm64\",\"build_version\":\"20260711\",",
            "\"customer_name\":\"测试客户\",\"features\":[\"采集\",\"analytics\"],",
            "\"key_id\":\"test-key\",\"package_id\":\"data-scientist-community-mac-arm64\",",
            "\"runtimes\":{\"collector\":{\"archive\":\"collector-runtime.tar.zst\",",
            "\"required_files\":[\"node_modules/playwright/package.json\",",
            "\"scripts/douyin_export.mjs\"],\"sha256\":\"",
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "\",\"size_bytes\":200,\"tree_sha256\":\"",
            "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
            "\",\"version\":\"collector_20260711\"},",
            "\"core\":{\"archive\":\"core-runtime.tar.zst\",",
            "\"required_files\":[\"frontend-compat/progress.html\",\"scripts/_run.py\"],",
            "\"sha256\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",",
            "\"size_bytes\":100,\"tree_sha256\":\"",
            "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "\",\"version\":\"core-20260711.1\"}}}"
        );
        assert_eq!(canonical, expected);
    }

    #[test]
    fn accepts_python_compatible_signature_and_extra_business_fields() {
        let manifest = verify(payload()).unwrap();
        assert_eq!(manifest.runtimes.core.version, "core-20260711.1");
        assert_eq!(manifest.runtimes.core.tree_sha256, "c".repeat(64));
        assert_eq!(manifest.package_id, "data-scientist-community-mac-arm64");
    }

    #[test]
    fn accepts_static_signature_generated_by_python_package_identity() {
        let manifest = serde_json::to_vec(&json!({
            "payload": payload(),
            "signature": "OGvwmbzJz_pcyv5uSvUVIpUGRvaQRcjk3jvw4lDmy8akhorvr4LR0Gf2H5avMzZDAAfeSFNpkhz0CriHq6jJDg"
        }))
        .unwrap();
        let keys = serde_json::to_vec(&json!({
            "active_key_id": "test-key",
            "keys": [{
                "key_id": "test-key",
                "public_key_pem": concat!(
                    "-----BEGIN PUBLIC KEY-----\n",
                    "MCowBQYDK2VwAyEA6kpsY+KcUgq+9VB7Ey7F+ZVHdq6+vnuSQh7qaRRG0iw=\n",
                    "-----END PUBLIC KEY-----\n"
                )
            }]
        }))
        .unwrap();

        let verified = verify_signed_manifest(&manifest, &keys).unwrap();
        assert_eq!(verified.runtimes.collector.version, "collector_20260711");
    }

    #[test]
    fn rejects_payload_tampering() {
        let signing = signing_key();
        let manifest = String::from_utf8(signed(payload(), &signing)).unwrap();
        for tampered in [
            manifest.replace("core-20260711.1", "core-20260711.2"),
            manifest.replace(&"c".repeat(64), &"e".repeat(64)),
        ] {
            assert!(matches!(
                verify_signed_manifest(tampered.as_bytes(), &key_bundle(&signing)),
                Err(ManifestError::SignatureInvalid)
            ));
        }
    }

    #[test]
    fn rejects_dangerous_versions() {
        for version in [
            "",
            ".",
            "..",
            ".tmp-active",
            "core.tmp-old",
            "core/v1",
            "core version",
            "核心-1",
        ] {
            let mut candidate = payload();
            candidate["runtimes"]["core"]["version"] = json!(version);
            assert!(matches!(verify(candidate), Err(ManifestError::Schema(_))));
        }
        let mut too_long = payload();
        too_long["runtimes"]["core"]["version"] = json!("x".repeat(129));
        assert!(matches!(verify(too_long), Err(ManifestError::Schema(_))));
    }

    #[test]
    fn rejects_noncanonical_required_files() {
        for required in [
            json!([]),
            json!(["scripts/runner.py", "scripts/_run.py"]),
            json!(["scripts/_run.py", "scripts/_run.py"]),
            json!(["../runner.py"]),
            json!(["scripts//runner.py"]),
            json!(["scripts/./runner.py"]),
            json!(["scripts\\runner.py"]),
            json!(["scripts/\0runner.py"]),
        ] {
            let mut candidate = payload();
            candidate["runtimes"]["core"]["required_files"] = required;
            assert!(matches!(verify(candidate), Err(ManifestError::Schema(_))));
        }
    }

    #[test]
    fn rejects_uppercase_sha_unknown_runtime_fields_and_missing_arm64() {
        let mut uppercase = payload();
        uppercase["runtimes"]["core"]["sha256"] = json!("A".repeat(64));
        assert!(matches!(verify(uppercase), Err(ManifestError::Schema(_))));

        let mut uppercase_tree = payload();
        uppercase_tree["runtimes"]["core"]["tree_sha256"] = json!("C".repeat(64));
        assert!(matches!(
            verify(uppercase_tree),
            Err(ManifestError::Schema(_))
        ));

        for invalid_tree in ["c".repeat(63), "c".repeat(65), "g".repeat(64)] {
            let mut invalid = payload();
            invalid["runtimes"]["core"]["tree_sha256"] = json!(invalid_tree);
            assert!(matches!(verify(invalid), Err(ManifestError::Schema(_))));
        }

        let mut missing_tree = payload();
        missing_tree["runtimes"]["collector"]
            .as_object_mut()
            .unwrap()
            .remove("tree_sha256");
        assert!(matches!(
            verify(missing_tree),
            Err(ManifestError::Schema(_))
        ));

        let mut descriptor_extra = payload();
        descriptor_extra["runtimes"]["core"]["url"] = json!("https://invalid");
        assert!(matches!(
            verify(descriptor_extra),
            Err(ManifestError::Schema(_))
        ));

        let mut runtime_extra = payload();
        runtime_extra["runtimes"]["legacy"] = runtime_extra["runtimes"]["core"].clone();
        assert!(matches!(
            verify(runtime_extra),
            Err(ManifestError::Schema(_))
        ));

        let mut no_arm64 = payload();
        no_arm64["arch"] = json!("x86_64");
        assert!(matches!(verify(no_arm64), Err(ManifestError::Schema(_))));
    }

    #[test]
    fn rejects_wrong_archive_zero_size_and_unsigned_envelope_fields() {
        let mut wrong_archive = payload();
        wrong_archive["runtimes"]["core"]["archive"] = json!("runtime.tar.zst");
        assert!(matches!(
            verify(wrong_archive),
            Err(ManifestError::Schema(_))
        ));

        let mut zero_size = payload();
        zero_size["runtimes"]["collector"]["size_bytes"] = json!(0);
        assert!(matches!(verify(zero_size), Err(ManifestError::Schema(_))));

        let signing = signing_key();
        let mut envelope: Value = serde_json::from_slice(&signed(payload(), &signing)).unwrap();
        envelope["unsigned_metadata"] = json!("not allowed");
        assert!(matches!(
            verify_signed_manifest(
                &serde_json::to_vec(&envelope).unwrap(),
                &key_bundle(&signing)
            ),
            Err(ManifestError::Json(_))
        ));
    }

    #[test]
    fn rejects_non_integer_numbers_and_unsafe_package_identifiers() {
        let mut float_metadata = payload();
        float_metadata["score"] = json!(1e-7);
        assert!(matches!(
            verify(float_metadata),
            Err(ManifestError::Schema(_))
        ));

        for (field, value) in [
            ("package_id", "../outside"),
            ("package_id", ".tmp-active"),
            ("build_version", "release.tmp-old"),
            ("build_version", "."),
            ("key_id", " test-key"),
        ] {
            let mut candidate = payload();
            candidate[field] = json!(value);
            assert!(matches!(verify(candidate), Err(ManifestError::Schema(_))));
        }

        let mut too_long = payload();
        too_long["package_id"] = json!("x".repeat(129));
        assert!(matches!(verify(too_long), Err(ManifestError::Schema(_))));
    }

    #[test]
    fn rejects_untrusted_and_duplicate_key_ids() {
        let signing = signing_key();
        let manifest = signed(payload(), &signing);
        let other_bundle = serde_json::to_vec(&json!({
            "keys": [{"key_id": "other", "public_key_pem": signing.verifying_key().to_public_key_pem(LineEnding::LF).unwrap()}]
        }))
        .unwrap();
        assert!(matches!(
            verify_signed_manifest(&manifest, &other_bundle),
            Err(ManifestError::KeyUntrusted)
        ));

        let pem = signing
            .verifying_key()
            .to_public_key_pem(LineEnding::LF)
            .unwrap();
        let duplicate_bundle = serde_json::to_vec(&json!({
            "keys": [
                {"key_id": "test-key", "public_key_pem": pem},
                {"key_id": "test-key", "public_key_pem": pem}
            ]
        }))
        .unwrap();
        assert!(matches!(
            verify_signed_manifest(&manifest, &duplicate_bundle),
            Err(ManifestError::KeyUntrusted)
        ));
    }

    #[test]
    fn verified_provenance_owns_the_signed_inputs() {
        let signing = signing_key();
        let mut manifest_bytes = signed(payload(), &signing);
        let mut key_bundle_bytes = key_bundle(&signing);
        let expected_manifest_bytes = manifest_bytes.clone();
        let expected_key_bundle_bytes = key_bundle_bytes.clone();

        let provenance =
            VerifiedPackageManifest::from_signed(&manifest_bytes, &key_bundle_bytes).unwrap();
        manifest_bytes.fill(b'x');
        key_bundle_bytes.fill(b'y');

        assert_eq!(provenance.manifest().package_id, "data-scientist-community-mac-arm64");
        assert_eq!(provenance.signed_bytes(), expected_manifest_bytes);
        assert_eq!(provenance.key_bundle_bytes(), expected_key_bundle_bytes);
        let cloned = provenance.clone();
        assert_eq!(cloned.manifest(), provenance.manifest());
        assert!(format!("{provenance:?}").contains("VerifiedPackageManifest"));
    }

    #[test]
    fn current_provenance_reverifies_an_old_envelope_with_current_keys() {
        let signing = signing_key();
        let current_bytes = signed(payload(), &signing);
        let current =
            VerifiedPackageManifest::from_signed(&current_bytes, &key_bundle(&signing)).unwrap();
        let mut previous_payload = payload();
        previous_payload["build_version"] = json!("20260710");
        let previous_bytes = signed(previous_payload, &signing);

        let previous = current.reverify(&previous_bytes).unwrap();
        assert_eq!(previous.manifest().build_version, "20260710");
        assert_eq!(previous.signed_bytes(), previous_bytes);
        assert_eq!(previous.key_bundle_bytes(), current.key_bundle_bytes());

        let tampered = String::from_utf8(previous_bytes)
            .unwrap()
            .replace("20260710", "20260709");
        assert!(matches!(
            current.reverify(tampered.as_bytes()),
            Err(ManifestError::SignatureInvalid)
        ));
    }

    #[test]
    fn provenance_rejects_oversized_manifest_and_key_inputs() {
        let signing = signing_key();
        let manifest = signed(payload(), &signing);
        let keys = key_bundle(&signing);
        let oversized_manifest = vec![b' '; 2 * 1024 * 1024 + 1];
        let oversized_keys = vec![b' '; 1024 * 1024 + 1];

        assert!(matches!(
            VerifiedPackageManifest::from_signed(&oversized_manifest, &keys),
            Err(ManifestError::InputTooLarge("manifest"))
        ));
        assert!(matches!(
            VerifiedPackageManifest::from_signed(&manifest, &oversized_keys),
            Err(ManifestError::InputTooLarge("key_bundle"))
        ));
        assert!(matches!(
            verify_signed_manifest(&oversized_manifest, &keys),
            Err(ManifestError::InputTooLarge("manifest"))
        ));
    }

    #[test]
    fn provenance_reverification_keeps_tree_hash_under_signature() {
        let signing = signing_key();
        let manifest = signed(payload(), &signing);
        let provenance =
            VerifiedPackageManifest::from_signed(&manifest, &key_bundle(&signing)).unwrap();
        let mut envelope: Value = serde_json::from_slice(provenance.signed_bytes()).unwrap();
        envelope["payload"]["runtimes"]["core"]["tree_sha256"] = json!("e".repeat(64));

        assert!(matches!(
            provenance.reverify(&serde_json::to_vec(&envelope).unwrap()),
            Err(ManifestError::SignatureInvalid)
        ));
    }
}
