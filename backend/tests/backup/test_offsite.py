"""app.backup.offsite against an in-memory S3 fake (no network, no account) and real GPG when available: disabled by
default, refuses crossed environments and insecure configuration, encrypts before upload, never overwrites or deletes,
verifies every object by reading it back, writes COMPLETE.json last and resumes a partial upload."""

import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.backup import offsite
from app.backup.sets import load
from tests.backup.helpers import env_for, needs_bash, run_script, sets_in, stub_bin


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """put / head / get / list only: there is no delete method, so any deletion attempt would fail the test."""

    def __init__(self):
        self.objects, self.puts, self.fail_put_on, self.corrupt = {}, [], None, set()

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError("404")
        return {"Metadata": self.objects[(Bucket, Key)][1]}

    def put_object(self, Bucket, Key, Body, ContentMD5, IfNoneMatch, Metadata):
        assert IfNoneMatch == "*"
        data = Body.read()
        import base64
        assert base64.b64encode(hashlib.md5(data).digest()).decode() == ContentMD5
        self.puts.append(Key)
        if self.fail_put_on is not None and len(self.puts) == self.fail_put_on:
            raise ClientError("InternalError")
        if (Bucket, Key) in self.objects:
            raise ClientError("PreconditionFailed")
        self.objects[(Bucket, Key)] = (data, dict(Metadata))

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError("NoSuchKey")
        data = self.objects[(Bucket, Key)][0]
        return {"Body": io.BytesIO(data + b"!" if Key in self.corrupt else data)}

    def get_paginator(self, name):
        objects = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for (b, k) in objects if b == Bucket and k.startswith(Prefix)]}
        return P()


def fake_encrypt(src: Path, dst: Path, recipient: Path, home: Path) -> None:
    dst.write_bytes(b"ENC:" + bytes(b ^ 0x5A for b in src.read_bytes()))


def config_file(tmp: Path, env="test", **over) -> Path:
    recipient = tmp / "recipient.asc"
    recipient.write_text("public key")
    values = {"ENABLED": "true", "ENVIRONMENT": env, "PREFIX": env, "ENDPOINT": "https://storage.example.invalid",
              "BUCKET": "hc-backups-test", "ACCESS_KEY_ID": "AKTEST", "SECRET_ACCESS_KEY": "not-a-real-secret",
              "GPG_RECIPIENT_FILE": str(recipient), "LOCAL_DIR": str(tmp / "backups"),
              "DATABASE": "hc_backuptest_a", **over}
    path = tmp / "offsite.env"
    path.write_text("".join(f"HYPERCHAT_OFFSITE_{k}={v}\n" for k, v in values.items() if v is not None))
    path.chmod(0o600)
    return path


@pytest.fixture
def local_set(tmp_path):
    if not shutil.which("bash") or not shutil.which("flock"):
        pytest.skip("needs bash and flock")
    bin_dir = stub_bin(tmp_path)
    (tmp_path / "test.witness").write_text('{"chain": "c1", "generation": 1, "state": "active"}\n')
    assert run_script(env_for(tmp_path, bin_dir)).returncode == 0
    (base,) = sets_in(tmp_path / "backups")
    return tmp_path, base


def run(argv, client):
    return offsite.main(argv, client_factory=lambda cfg: client, encrypt=fake_encrypt)


# ── configuration ───────────────────────────────────────────────────────────────

def test_the_template_is_disabled_and_holds_no_value(tmp_path):
    template = Path(offsite.__file__).resolve().parents[3] / "deploy/backup/offsite.env.example"
    copy = tmp_path / "offsite.env"
    copy.write_text(template.read_text())
    copy.chmod(0o600)
    with pytest.raises(offsite.Refused, match="offsite_disabled"):
        offsite.read_config(str(copy))
    for line in template.read_text().splitlines():
        if line.startswith("HYPERCHAT_OFFSITE_") and not line.startswith("HYPERCHAT_OFFSITE_ENABLED"):
            assert line.endswith("="), line


@pytest.mark.parametrize("over,reason", [
    ({"ENABLED": "false"}, "offsite_disabled"),
    ({"PREFIX": "prod"}, "prefix_must_be_the_environment"),
    ({"ENVIRONMENT": "staging", "PREFIX": "staging"}, "config_environment"),
    ({"ENDPOINT": "http://storage.example.invalid"}, "endpoint_must_be_https"),
    ({"ENDPOINT": "https://x.invalid/../../etc"}, "endpoint_must_be_https"),
    ({"BUCKET": "Bad_Bucket"}, "config_bucket"),
    ({"SECRET_ACCESS_KEY": ""}, "config_credentials"),
    ({"GPG_RECIPIENT_FILE": "relative.asc"}, "config_gpg_recipient_file"),
    ({"LOCAL_DIR": "/var/backups/hyperchat"}, "config_test_scope"),
    ({"DATABASE": "hyperchat"}, "config_test_scope"),
])
def test_unsafe_or_crossed_configuration_is_refused(tmp_path, over, reason):
    with pytest.raises(offsite.Refused, match=reason):
        offsite.read_config(str(config_file(tmp_path, **over)))


@pytest.mark.parametrize("env,over", [
    ("prod", {"LOCAL_DIR": "/var/backups/hyperchat-dev"}),
    ("prod", {"DATABASE": "hyperchat_dev", "LOCAL_DIR": "/var/backups/hyperchat"}),
    ("dev", {"LOCAL_DIR": "/var/backups/hyperchat", "DATABASE": "hyperchat_dev"}),
])
def test_production_and_dev_never_read_each_others_backups(tmp_path, env, over):
    with pytest.raises(offsite.Refused, match="another_environment"):
        offsite.read_config(str(config_file(tmp_path, env=env, **over)))


def test_a_readable_config_is_refused_and_credentials_never_print(tmp_path):
    path = config_file(tmp_path)
    path.chmod(0o644)
    with pytest.raises(offsite.Refused, match="config_must_be_a_private_file"):
        offsite.read_config(str(path))
    path.chmod(0o600)
    assert "not-a-real-secret" not in repr(offsite.read_config(str(path)))


# ── upload, verify, resume ──────────────────────────────────────────────────────

def test_a_verified_set_is_uploaded_encrypted_verified_and_marked_complete_last(local_set, capsys):
    tmp, base = local_set
    s3 = FakeS3()
    assert run(["upload", "--config", str(config_file(tmp)), "--latest"], s3) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and {o["outcome"] for o in out["objects"]} == {"uploaded"}
    keys = s3.puts
    assert keys[-1] == f"test/{base}/COMPLETE.json" and all(k.startswith(f"test/{base}/") for k in keys)
    assert {k.rsplit("/", 1)[1] for k in keys} == {f"{base}.dump.gpg", f"{base}.dump.sha256.gpg",
                                                  f"{base}.witness.gpg", f"{base}.manifest.json.gpg", "COMPLETE.json"}
    local = load(tmp / "backups", base)
    for path in local.files:  # nothing leaves in plaintext
        assert s3.objects[("hc-backups-test", f"test/{base}/{path.name}.gpg")][0] != path.read_bytes()
    marker = json.loads(s3.objects[("hc-backups-test", f"test/{base}/COMPLETE.json")][0])
    assert marker["environment"] == "test" and len(marker["objects"]) == 4
    assert "not-a-real-secret" not in json.dumps(marker)
    assert run(["verify", "--config", str(config_file(tmp)), "--set", base], s3) == 0
    assert run(["list", "--config", str(config_file(tmp))], s3) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["sets"] == [base]


def test_a_partial_upload_has_no_marker_and_a_rerun_resumes_without_replacing(local_set, capsys):
    tmp, base = local_set
    s3 = FakeS3()
    s3.fail_put_on = 3
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 1
    assert ("hc-backups-test", f"test/{base}/COMPLETE.json") not in s3.objects
    assert run(["verify", "--config", str(config_file(tmp)), "--set", base], s3) == 1  # incomplete off-site
    capsys.readouterr()
    s3.fail_put_on = None
    stored_before = {k: v[0] for k, v in s3.objects.items()}
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 0
    outcomes = {o["key"].rsplit("/", 1)[1]: o["outcome"] for o in json.loads(capsys.readouterr().out)["objects"]}
    assert outcomes[f"{base}.dump.gpg"] == "kept" and "uploaded" in outcomes.values()
    for key, data in stored_before.items():
        assert s3.objects[key][0] == data  # nothing replaced


def test_an_object_that_reads_back_differently_fails_and_leaves_no_marker(local_set):
    tmp, base = local_set
    s3 = FakeS3()
    s3.corrupt.add(f"test/{base}/{base}.dump.gpg")
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 1
    assert ("hc-backups-test", f"test/{base}/COMPLETE.json") not in s3.objects


def test_an_existing_object_with_other_content_is_never_overwritten(local_set):
    tmp, base = local_set
    s3 = FakeS3()
    s3.objects[("hc-backups-test", f"test/{base}/{base}.dump.gpg")] = (b"someone else's", {"sha256": "0" * 64})
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 2
    assert s3.objects[("hc-backups-test", f"test/{base}/{base}.dump.gpg")][0] == b"someone else's"


def test_an_invalid_local_set_is_never_uploaded(local_set):
    tmp, base = local_set
    dump = tmp / "backups" / f"{base}.dump"
    dump.write_bytes(dump.read_bytes() + b"tampered")
    s3 = FakeS3()
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 2 and s3.puts == []
    assert run(["upload", "--config", str(config_file(tmp)), "--set", "../../etc/passwd"], s3) == 2 and s3.puts == []


def test_verify_refuses_a_marker_pointing_outside_its_set(local_set):
    tmp, base = local_set
    s3 = FakeS3()
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 0
    key = ("hc-backups-test", f"test/{base}/COMPLETE.json")
    marker = json.loads(s3.objects[key][0])
    marker["objects"][0]["key"] = "prod/other/x.gpg"
    s3.objects[key] = (json.dumps(marker).encode(), {})
    assert run(["verify", "--config", str(config_file(tmp)), "--set", base], s3) == 2


def test_fetch_downloads_a_complete_set_verified_and_still_encrypted(local_set):
    tmp, base = local_set
    s3 = FakeS3()
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 0
    to = tmp / "fetched"
    assert run(["fetch", "--config", str(config_file(tmp)), "--set", base, "--to", str(to)], s3) == 0
    marker = json.loads((to / "COMPLETE.json").read_text())
    for obj in marker["objects"]:
        data = (to / obj["key"].rsplit("/", 1)[1]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == obj["sha256"] and data.startswith(b"ENC:")
    assert oct(to.stat().st_mode)[-3:] == "700"
    assert run(["fetch", "--config", str(config_file(tmp)), "--set", base, "--to", str(to)], s3) == 2  # never reused
    s3.corrupt.add(f"test/{base}/{base}.dump.gpg")
    assert run(["fetch", "--config", str(config_file(tmp)), "--set", base, "--to", str(tmp / "again")], s3) == 1


def test_the_tool_has_no_way_to_delete():
    source = Path(offsite.__file__).read_text()
    for call in ("delete_object", "delete_objects", "DeleteObject", "abort_multipart", "put_bucket_lifecycle"):
        assert call not in source, call


@needs_bash
@pytest.mark.skipif(not shutil.which("gpg"), reason="needs gpg")
def test_real_gpg_encryption_round_trips_only_with_the_offline_private_key(local_set, tmp_path_factory):
    tmp, base = local_set
    keyhome = tmp_path_factory.mktemp("owner-gnupg")
    os.chmod(keyhome, 0o700)
    gpg = ["gpg", "--homedir", str(keyhome), "--batch", "--yes", "--no-tty", "--quiet", "--pinentry-mode", "loopback",
           "--passphrase", ""]
    subprocess.run([*gpg, "--quick-gen-key", "Backup Owner <owner@example.invalid>", "default", "default", "never"],
                   check=True, capture_output=True)
    public = tmp / "owner-public.asc"
    public.write_bytes(subprocess.run([*gpg, "--armor", "--export"], check=True, capture_output=True).stdout)
    other = tmp_path_factory.mktemp("no-key")
    os.chmod(other, 0o700)
    try:
        s3 = FakeS3()
        cfg = offsite.read_config(str(config_file(tmp)))
        cfg = offsite.Config(**{**cfg.__dict__, "recipient_file": public})
        offsite.upload(cfg, load(tmp / "backups", base), s3)  # the real gpg_encrypt
        enc = s3.objects[("hc-backups-test", f"test/{base}/{base}.dump.gpg")][0]
        (tmp / "x.gpg").write_bytes(enc)
        plain = subprocess.run([*gpg, "--decrypt", str(tmp / "x.gpg")], check=True, capture_output=True).stdout
        assert plain == (tmp / "backups" / f"{base}.dump").read_bytes()
        assert subprocess.run(["gpg", "--homedir", str(other), "--batch", "--decrypt", str(tmp / "x.gpg")],
                              capture_output=True).returncode != 0  # without the owner's key: unreadable
    finally:
        for home in (keyhome, other):  # no gpg-agent left running after the test
            subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"], capture_output=True)
