#!/usr/bin/env python3
"""Generate a deterministic SPDX 2.3 JSON SBOM from repository lock files."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PYTHON_LICENSE_FALLBACKS = {
    "certifi": "MPL-2.0",
    "cffi": "MIT",
    "cryptography": "Apache-2.0 OR BSD-3-Clause",
    "et-xmlfile": "MIT",
    "numpy": "BSD-3-Clause",
    "openpyxl": "MIT",
    "pandas": "BSD-3-Clause",
    "pycparser": "BSD-3-Clause",
    "python-dateutil": "Apache-2.0 OR BSD-3-Clause",
    "six": "MIT",
}


def clean_license(value: str | None) -> str:
    text = str(value or "").strip()
    if not text or text.upper() in {"UNKNOWN", "NOASSERTION"}:
        return "NOASSERTION"
    if "\n" in text or len(text) > 200:
        return "NOASSERTION"
    # Older Cargo manifests commonly use a slash for dual licensing.  Cargo
    # treats these as alternatives; SPDX requires an explicit OR operator.
    text = re.sub(r"\s*/\s*", " OR ", text)
    return text


def spdx_id(ecosystem: str, name: str, version: str) -> str:
    raw = f"{ecosystem}-{name}-{version}"
    safe = re.sub(r"[^A-Za-z0-9.-]+", "-", raw).strip("-.")
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"SPDXRef-Package-{safe[:80]}-{suffix}"


def package_record(
    ecosystem: str,
    name: str,
    version: str,
    license_expression: str,
    download_location: str = "NOASSERTION",
    checksum: str = "",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "SPDXID": spdx_id(ecosystem, name, version),
        "name": name,
        "versionInfo": version,
        "downloadLocation": download_location or "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": clean_license(license_expression),
        "licenseDeclared": clean_license(license_expression),
        "supplier": "NOASSERTION",
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": f"pkg:{ecosystem}/{name}@{version}",
            }
        ],
    }
    if checksum and re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
        record["checksums"] = [{"algorithm": "SHA256", "checksumValue": checksum.lower()}]
    return record


def npm_name_from_path(path: str, payload: dict[str, Any]) -> str:
    explicit = str(payload.get("name") or "").strip()
    if explicit:
        return explicit
    marker = "node_modules/"
    tail = path.rsplit(marker, 1)[-1]
    parts = tail.split("/")
    if parts and parts[0].startswith("@") and len(parts) > 1:
        return "/".join(parts[:2])
    return parts[0] if parts else tail


def load_npm(lock_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    records = []
    for path, item in sorted((payload.get("packages") or {}).items()):
        if not path or not isinstance(item, dict):
            continue
        name = npm_name_from_path(path, item)
        version = str(item.get("version") or "").strip()
        if not name or not version:
            continue
        records.append(
            package_record(
                "npm",
                name,
                version,
                str(item.get("license") or "NOASSERTION"),
                str(item.get("resolved") or "NOASSERTION"),
            )
        )
    return records


def parse_requirements(path: Path) -> list[tuple[str, str]]:
    requirements = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+)", line)
        if not match:
            raise ValueError(f"requirement must be exactly pinned: {raw}")
        requirements.append((match.group(1), match.group(2)))
    return requirements


def python_license(name: str) -> str:
    normalized = name.lower().replace("_", "-")
    if normalized in PYTHON_LICENSE_FALLBACKS:
        return PYTHON_LICENSE_FALLBACKS[normalized]
    try:
        metadata = importlib.metadata.metadata(name)
        expression = metadata.get("License-Expression") or metadata.get("License")
        if expression and str(expression).strip().upper() not in {"UNKNOWN", "NOASSERTION"}:
            cleaned = clean_license(str(expression))
            if cleaned != "NOASSERTION":
                return cleaned
    except importlib.metadata.PackageNotFoundError:
        pass
    return "NOASSERTION"


def load_python(requirements_path: Path) -> list[dict[str, Any]]:
    return [
        package_record(
            "pypi",
            name,
            version,
            python_license(name),
            f"https://pypi.org/project/{name}/{version}/",
        )
        for name, version in parse_requirements(requirements_path)
    ]


def load_cargo_metadata_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for item in sorted(payload.get("packages") or [], key=lambda value: (value.get("name", ""), value.get("version", ""))):
        if not item.get("source"):
            continue
        name = str(item.get("name") or "")
        version = str(item.get("version") or "")
        source = str(item.get("source") or "NOASSERTION")
        if source.startswith("registry+"):
            download_location = f"https://crates.io/api/v1/crates/{name}/{version}/download"
        elif source.startswith("git+"):
            download_location = source.removeprefix("git+").split("#", 1)[0]
        else:
            download_location = "NOASSERTION"
        records.append(
            package_record(
                "cargo",
                name,
                version,
                str(item.get("license") or "NOASSERTION"),
                download_location,
            )
        )
    return records


def load_cargo_metadata(path: Path) -> list[dict[str, Any]]:
    return load_cargo_metadata_payload(json.loads(path.read_text(encoding="utf-8")))


def load_cargo_metadata_from_tool(root: Path) -> list[dict[str, Any]]:
    node = str(os.environ.get("npm_node_execpath") or "").strip() or shutil.which("node")
    rust_tool = root / "scripts" / "run_rust_tool.mjs"
    manifest = root / "desktop" / "src-tauri" / "Cargo.toml"
    if not node:
        raise RuntimeError("Node.js is required to resolve Cargo license metadata")
    if not rust_tool.is_file() or not manifest.is_file():
        raise RuntimeError("Cargo metadata inputs are missing")
    result = subprocess.run(
        [
            node,
            str(rust_tool),
            "cargo",
            "metadata",
            "--format-version",
            "1",
            "--locked",
            "--manifest-path",
            str(manifest),
        ],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Cargo metadata did not return valid JSON") from exc
    return load_cargo_metadata_payload(payload)


def dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in records:
        ref = (item["SPDXID"], item["name"], item["versionInfo"])
        current = unique.get(ref)
        if current is None or current.get("licenseDeclared") == "NOASSERTION":
            unique[ref] = item
    return sorted(unique.values(), key=lambda item: (item["name"].lower(), item["versionInfo"], item["SPDXID"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--cargo-metadata-json")
    parser.add_argument("--output", default="SBOM.spdx.json")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    packages: list[dict[str, Any]] = []
    packages.extend(load_npm(root / "package-lock.json"))
    packages.extend(load_npm(root / "desktop/package-lock.json"))
    packages.extend(load_python(root / "requirements.txt"))
    if args.cargo_metadata_json:
        packages.extend(load_cargo_metadata(Path(args.cargo_metadata_json)))
    else:
        packages.extend(load_cargo_metadata_from_tool(root))
    packages = dedupe(packages)
    unknown = sum(item.get("licenseDeclared") == "NOASSERTION" for item in packages)
    if unknown:
        raise RuntimeError(
            f"refusing to write incomplete SBOM: {unknown} dependency licenses are NOASSERTION"
        )

    root_id = "SPDXRef-Package-data-scientist-community"
    root_package = {
        "SPDXID": root_id,
        "name": "data-scientist-community",
        "versionInfo": "0.1.0",
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "AGPL-3.0-only",
        "licenseDeclared": "AGPL-3.0-only",
        "supplier": "NOASSERTION",
    }
    identity = "|".join(f"{item['SPDXID']}:{item['licenseDeclared']}" for item in packages)
    namespace_uuid = uuid.uuid5(uuid.NAMESPACE_URL, identity)
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "data-scientist-community-0.1.0",
        "documentNamespace": f"urn:uuid:{namespace_uuid}",
        "creationInfo": {
            "created": "2026-08-05T00:00:00Z",
            "creators": ["Tool: scripts/generate_sbom.py"],
            "licenseListVersion": "3.27",
        },
        "packages": [root_package, *packages],
        "relationships": [
            {"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": root_id},
            *[
                {"spdxElementId": root_id, "relationshipType": "DEPENDS_ON", "relatedSpdxElement": item["SPDXID"]}
                for item in packages
            ],
        ],
    }
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output), "packages": len(packages), "unknown_licenses": unknown}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
