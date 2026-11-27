"""app.backup.offsite against an in-memory S3 fake (no network, no account) and real GPG when available: disabled by
default, refuses crossed environments and insecure configuration, encrypts before upload, never overwrites or deletes,
verifies every object by reading it back, writes COMPLETE.json last and resumes a partial upload. Every set is signed
(COMPLETE.json.sig, ssh-keygen ed25519) and nothing unsigned, forged or future-stamped is listed, verified or fetched
(security audit run-4, NV4-4)."""

import hashlib
import io
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
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
        self.uploaded = {}  # the provider's own LastModified per key (objects placed by a test count as "now")

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError("404")
        return {"Metadata": self.objects[(Bucket, Key)][1],
                "LastModified": self.uploaded.get((Bucket, Key), datetime.now(UTC))}

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
        self.uploaded[(Bucket, Key)] = datetime.now(UTC)

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
    """RANDOMIZED like GPG (a fresh session key per call): the same plaintext never encrypts to the same bytes twice,
    so a resume can only succeed by reusing the ciphertext it already produced."""
    dst.write_bytes(b"ENC:" + os.urandom(16) + bytes(b ^ 0x5A for b in src.read_bytes()))


def signing_keys(tmp: Path, name="sign", principal="hyperchat-offsite-test") -> tuple[Path, Path]:
    """An ed25519 signing key (0600) and its allowed-signers file, as the owner sets them up per environment."""
    key, allowed = tmp / f"{name}.key", tmp / f"{name}.allowed"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", principal, "-f", str(key)], check=True)
        key.chmod(0o600)
        kind, blob = (tmp / f"{name}.key.pub").read_text().split()[:2]
        allowed.write_text(f'{principal} namespaces="hyperchat-offsite" {kind} {blob}\n')
        allowed.chmod(0o644)
    return key, allowed


def ssh_sign(key: Path, data: bytes) -> bytes:
    return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", "hyperchat-offsite"], input=data,
                          check=True, capture_output=True).stdout


def config_file(tmp: Path, env="test", **over) -> Path:
    recipient = tmp / "recipient.asc"
    recipient.write_text("public key")
    key, allowed = signing_keys(tmp) if env == "test" else (None, None)
    values = {"ENABLED": "true", "ENVIRONMENT": env, "PREFIX": env, "ENDPOINT": "https://storage.example.invalid",
              "BUCKET": "hc-backups-test", "ACCESS_KEY_ID": "AKTEST", "SECRET_ACCESS_KEY": "not-a-real-secret",
              "GPG_RECIPIENT_FILE": str(recipient), "LOCAL_DIR": str(tmp / "backups"),
              "DATABASE": "hc_backuptest_a", "STATE_DIR": str(tmp / "state" / env),
              "SIGNING_KEY_FILE": key and str(key), "ALLOWED_SIGNERS_FILE": allowed and str(allowed), **over}
    (tmp / "state").mkdir(mode=0o700, exist_ok=True)
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


def resign(tmp: Path, s3: FakeS3, base: str, marker: dict) -> None:
    """Replace a set's marker AND sign it with the genuine key: a test of a later check must get past the signature."""
    data = json.dumps(marker, sort_keys=True).encode()
    s3.objects[("hc-backups-test", f"test/{base}/COMPLETE.json")] = (data, {})
    s3.objects[("hc-backups-test", f"test/{base}/COMPLETE.json.sig")] = (ssh_sign(signing_keys(tmp)[0], data), {})


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
    ({"STATE_DIR": "/var/lib/hyperchat-offsite/test"}, "config_test_scope"),
    ({"STATE_DIR": "relative/test"}, "config_state_dir"),
    ({"STATE_DIR": "/tmp/state/prod"}, "config_state_dir"),  # another environment's state directory
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
    assert keys[-2] == f"test/{base}/COMPLETE.json.sig"  # signed before the marker says "complete"
    assert {k.rsplit("/", 1)[1] for k in keys} == {f"{base}.dump.gpg", f"{base}.dump.sha256.gpg",
                                                  f"{base}.witness.gpg", f"{base}.manifest.json.gpg", "COMPLETE.json",
                                                  "COMPLETE.json.sig"}
    local = load(tmp / "backups", base)
    for path in local.files:  # nothing leaves in plaintext
        assert s3.objects[("hc-backups-test", f"test/{base}/{path.name}.gpg")][0] != path.read_bytes()
    marker = json.loads(s3.objects[("hc-backups-test", f"test/{base}/COMPLETE.json")][0])
    assert marker["environment"] == "test" and len(marker["objects"]) == 4
    assert "not-a-real-secret" not in json.dumps(marker)
    assert run(["verify", "--config", str(config_file(tmp)), "--set", base], s3) == 0
    assert run(["list", "--config", str(config_file(tmp))], s3) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"ok": True, "sets": [base], "refused": {}}


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


# ── resuming safely (review of PR #79) ──────────────────────────────────────────

def _partial(tmp, base, fail_on=3) -> FakeS3:
    s3 = FakeS3()
    s3.fail_put_on = fail_on
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 1
    s3.fail_put_on = None
    return s3


def test_metadata_alone_never_vouches_for_a_remote_object(local_set):
    tmp, base = local_set
    s3 = _partial(tmp, base)
    key = ("hc-backups-test", f"test/{base}/{base}.dump.gpg")
    data, meta = s3.objects[key]
    s3.objects[key] = (b"forged bytes with the right metadata", meta)  # metadata still names the right hash
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 2
    assert s3.objects[key][0] == b"forged bytes with the right metadata"  # refused, not overwritten
    assert ("hc-backups-test", f"test/{base}/COMPLETE.json") not in s3.objects


def test_a_resume_without_its_local_records_refuses_instead_of_guessing(local_set):
    tmp, base = local_set
    s3 = _partial(tmp, base)
    stored = {k: v[0] for k, v in s3.objects.items()}
    shutil.rmtree(tmp / "state" / "test" / base)  # the local records are gone: the remote objects are unverifiable
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 2
    assert {k: v[0] for k, v in s3.objects.items() if k in stored} == stored
    assert ("hc-backups-test", f"test/{base}/COMPLETE.json") not in s3.objects


def test_the_binding_records_refuse_a_changed_plaintext_or_recipient(local_set):
    tmp, base = local_set
    cfg = offsite.read_config(str(config_file(tmp)))
    s3 = FakeS3()
    s3.fail_put_on = 2
    with pytest.raises(offsite.Failed):
        offsite.upload(cfg, load(tmp / "backups", base), s3, fake_encrypt)
    s3.fail_put_on = None
    # someone rewrites the set consistently (dump, checksum and manifest): load() accepts it, the records do not
    import hashlib as h
    d = tmp / "backups"
    body = b"PGDMP replaced archive"
    (d / f"{base}.dump").write_bytes(body)
    sha = h.sha256(body).hexdigest()
    (d / f"{base}.dump.sha256").write_text(f"{sha}  {base}.dump\n")
    m = json.loads((d / f"{base}.manifest.json").read_text())
    m["dump"]["sha256"] = sha
    (d / f"{base}.manifest.json").write_text(json.dumps(m))
    with pytest.raises(offsite.Refused, match="local_plaintext_changed"):
        offsite.upload(cfg, load(d, base), s3, fake_encrypt)
    other = tmp / "other.asc"
    other.write_text("another public key")
    cfg2 = offsite.Config(**{**cfg.__dict__, "recipient_file": other})
    state = tmp / "state" / "test" / base
    record = json.loads((state / f"{base}.dump.json").read_text())
    record["plain_sha256"] = sha  # even with the plaintext matching, another recipient cannot continue
    (state / f"{base}.dump.json").write_text(json.dumps(record))
    with pytest.raises(offsite.Refused, match="recipient_changed"):
        offsite.upload(cfg2, load(d, base), s3, fake_encrypt)


def test_a_record_of_another_set_or_a_changed_local_ciphertext_is_refused(local_set):
    tmp, base = local_set
    cfg = offsite.read_config(str(config_file(tmp)))
    s3 = FakeS3()
    s3.fail_put_on = 2
    with pytest.raises(offsite.Failed):
        offsite.upload(cfg, load(tmp / "backups", base), s3, fake_encrypt)
    s3.fail_put_on = None
    state = tmp / "state" / "test" / base
    enc = state / f"{base}.dump.gpg"
    original = enc.read_bytes()
    enc.write_bytes(original + b"x")
    with pytest.raises(offsite.Refused, match="local_ciphertext_changed"):
        offsite.upload(cfg, load(tmp / "backups", base), s3, fake_encrypt)
    enc.write_bytes(original)
    record = json.loads((state / f"{base}.dump.json").read_text())
    (state / f"{base}.dump.json").write_text(json.dumps({**record, "set": "hc_backuptest_a_20200101T000000Z"}))
    with pytest.raises(offsite.Refused, match="local_state_of_another_set"):
        offsite.upload(cfg, load(tmp / "backups", base), s3, fake_encrypt)


def test_an_interrupted_marker_is_retried_with_identical_bytes_and_the_state_is_cleaned(local_set):
    tmp, base = local_set
    s3 = _partial(tmp, base, fail_on=6)  # every object and the signature stored, the COMPLETE marker failed
    assert ("hc-backups-test", f"test/{base}/COMPLETE.json.sig") in s3.objects
    assert ("hc-backups-test", f"test/{base}/COMPLETE.json") not in s3.objects
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 0
    assert ("hc-backups-test", f"test/{base}/COMPLETE.json") in s3.objects
    assert not (tmp / "state" / "test" / base).exists()  # removed once complete


def test_an_already_complete_set_is_verified_not_uploaded_again(local_set, capsys):
    tmp, base = local_set
    s3 = FakeS3()
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 0
    puts = list(s3.puts)
    capsys.readouterr()
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 0
    assert json.loads(capsys.readouterr().out)["already_complete"] is True and s3.puts == puts
    s3.corrupt.add(f"test/{base}/{base}.witness.gpg")
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 1  # a damaged off-site set
    s3.corrupt.clear()
    cfg_path = config_file(tmp)
    (tmp / "recipient.asc").write_text("another public key")  # the complete set was encrypted to another key
    assert run(["upload", "--config", str(cfg_path), "--set", base], s3) == 2


@pytest.mark.parametrize("field, value", [("database", "hc_backuptest_other"), ("plain_sha256", "0" * 64)])
def test_a_complete_marker_of_other_content_is_refused_not_trusted(local_set, field, value):
    """A COMPLETE.json under this set's key that names another database, or other plaintext, is never accepted."""
    tmp, base = local_set
    s3 = FakeS3()
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 0
    key = ("hc-backups-test", f"test/{base}/COMPLETE.json")
    data, meta = s3.objects[key]
    marker = json.loads(data)
    if field == "database":
        marker["database"] = value
    else:
        marker["objects"][0]["plain_sha256"] = value
    resign(tmp, s3, base, marker)  # even correctly signed, other content is refused
    puts = list(s3.puts)
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 2 and s3.puts == puts


def test_the_state_directory_must_be_private_and_never_a_symlink(local_set):
    tmp, base = local_set
    s3 = FakeS3()
    cfg_path = config_file(tmp)
    state = tmp / "state" / "test"
    state.mkdir()
    state.chmod(0o755)
    assert run(["upload", "--config", str(cfg_path), "--set", base], s3) == 2 and s3.puts == []
    state.rmdir()
    (tmp / "elsewhere").mkdir(mode=0o700)
    state.symlink_to(tmp / "elsewhere")
    assert run(["upload", "--config", str(cfg_path), "--set", base], s3) == 2 and s3.puts == []


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
    resign(tmp, s3, base, marker)
    assert run(["verify", "--config", str(config_file(tmp)), "--set", base], s3) == 2


def test_fetch_downloads_a_complete_set_verified_and_still_encrypted(local_set):
    tmp, base = local_set
    s3 = FakeS3()
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 0
    to = tmp / "fetched"
    assert run(["fetch", "--config", str(config_file(tmp)), "--set", base, "--to", str(to)], s3) == 0
    marker = json.loads((to / "COMPLETE.json").read_text())
    allowed = signing_keys(tmp)[1]
    assert subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "hyperchat-offsite-test", "-n",
                           "hyperchat-offsite", "-s", str(to / "COMPLETE.json.sig")],
                          input=(to / "COMPLETE.json").read_bytes(), capture_output=True).returncode == 0
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


@pytest.fixture
def owner_key(tmp_path_factory):
    """A real GPG key pair standing in for the owner's offline key; (public key file, gpg command for decryption)."""
    if not shutil.which("gpg"):
        pytest.skip("needs gpg")
    home = tmp_path_factory.mktemp("owner-gnupg")
    os.chmod(home, 0o700)
    gpg = ["gpg", "--homedir", str(home), "--batch", "--yes", "--no-tty", "--quiet", "--pinentry-mode", "loopback",
           "--passphrase", ""]
    subprocess.run([*gpg, "--quick-gen-key", "Backup Owner <owner@example.invalid>", "default", "default", "never"],
                   check=True, capture_output=True)
    public = home.parent / f"{home.name}-public.asc"
    public.write_bytes(subprocess.run([*gpg, "--armor", "--export"], check=True, capture_output=True).stdout)
    yield public, gpg
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"], capture_output=True)


def real_config(tmp: Path, public: Path) -> offsite.Config:
    cfg = offsite.read_config(str(config_file(tmp)))
    return offsite.Config(**{**cfg.__dict__, "recipient_file": public})


def test_real_gpg_an_interrupted_upload_resumes_and_completes(local_set, owner_key):
    """Review of PR #79 (reproduced first): GPG encryption is randomized, so re-encrypting on a retry produces other
    ciphertext; the resume must still recognise the objects it already uploaded and complete the set."""
    tmp, base = local_set
    public, gpg = owner_key
    cfg = real_config(tmp, public)
    s3 = FakeS3()
    s3.fail_put_on = 3
    with pytest.raises(offsite.Failed):
        offsite.upload(cfg, load(tmp / "backups", base), s3)
    uploaded_before = {k: v[0] for k, v in s3.objects.items()}
    assert len(uploaded_before) == 2
    s3.fail_put_on = None
    result = offsite.upload(cfg, load(tmp / "backups", base), s3)
    assert result["ok"] and [o["outcome"] for o in result["objects"]].count("kept") == 2
    for key, data in uploaded_before.items():
        assert s3.objects[key][0] == data  # nothing replaced
    enc = s3.objects[("hc-backups-test", f"test/{base}/{base}.dump.gpg")][0]
    (tmp / "x.gpg").write_bytes(enc)
    plain = subprocess.run([*gpg, "--decrypt", str(tmp / "x.gpg")], check=True, capture_output=True).stdout
    assert plain == (tmp / "backups" / f"{base}.dump").read_bytes()


def test_real_gpg_a_resume_without_records_refuses_and_a_complete_set_is_only_verified(local_set, owner_key):
    tmp, base = local_set
    public, _ = owner_key
    cfg = real_config(tmp, public)
    s3 = FakeS3()
    s3.fail_put_on = 2
    with pytest.raises(offsite.Failed):
        offsite.upload(cfg, load(tmp / "backups", base), s3)
    s3.fail_put_on = None
    state = tmp / "state" / "test" / base
    saved = tmp / "saved-state"
    shutil.copytree(state, saved)
    shutil.rmtree(state)  # fresh GPG ciphertext can never match what is already stored
    with pytest.raises(offsite.Refused, match="remote_object_differs_from_this_sets_ciphertext"):
        offsite.upload(cfg, load(tmp / "backups", base), s3)
    shutil.rmtree(state)
    shutil.copytree(saved, state)  # with its records the same upload completes
    assert offsite.upload(cfg, load(tmp / "backups", base), s3)["ok"]
    puts = list(s3.puts)
    assert offsite.upload(cfg, load(tmp / "backups", base), s3)["already_complete"] and s3.puts == puts


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


# ── signed sets (security audit run-4, NV4-4) ─────────────────────────────────────────────────────────────────────────

def _plant(s3: FakeS3, genuine: str, stamp: str, sig: bytes | None) -> str:
    """What a bucket-token holder can do without the signing key: copy a genuine set's objects under a new set name
    with a self-consistent marker (every hash matches what it names), optionally with a signature of their own."""
    base = f"hc_backuptest_a_{stamp}"
    marker = json.loads(s3.objects[("hc-backups-test", f"test/{genuine}/COMPLETE.json")][0])
    for obj in marker["objects"]:
        data = s3.objects[("hc-backups-test", obj["key"])][0]
        obj["key"] = obj["key"].replace(genuine, base)
        obj["plain_name"] = obj["plain_name"].replace(genuine, base)
        s3.objects[("hc-backups-test", obj["key"])] = (data, {})
    marker.update(set=base, created_at=stamp)
    data = json.dumps(marker, sort_keys=True).encode()
    s3.objects[("hc-backups-test", f"test/{base}/COMPLETE.json")] = (data, {})
    if sig is not None:
        s3.objects[("hc-backups-test", f"test/{base}/COMPLETE.json.sig")] = (sig, {})
    return base


@pytest.mark.parametrize("signature", ["none", "own key", "genuine signature of another marker"])
def test_a_planted_newest_set_is_never_listed_verified_or_fetched(local_set, capsys, signature):
    tmp, genuine = local_set
    s3 = FakeS3()
    assert run(["upload", "--config", str(config_file(tmp)), "--set", genuine], s3) == 0
    stamp = (datetime.now(UTC) + timedelta(minutes=1)).strftime("%Y%m%dT%H%M%SZ")  # newer, within the clock skew
    genuine_sig = s3.objects[("hc-backups-test", f"test/{genuine}/COMPLETE.json.sig")][0]
    planted = _plant(s3, genuine, stamp, genuine_sig if signature.startswith("genuine") else None)
    if signature == "own key":  # the attacker signs their own marker with a key of their own
        data = s3.objects[("hc-backups-test", f"test/{planted}/COMPLETE.json")][0]
        s3.objects[("hc-backups-test", f"test/{planted}/COMPLETE.json.sig")] = (
            ssh_sign(signing_keys(tmp, name="attacker")[0], data), {})
    capsys.readouterr()
    assert run(["list", "--config", str(config_file(tmp))], s3) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sets"] == [genuine]
    assert out["refused"] == {planted: "offsite_set_unsigned" if signature == "none" else "offsite_set_signature_invalid"}
    assert run(["verify", "--config", str(config_file(tmp)), "--set", planted], s3) == 2
    assert run(["fetch", "--config", str(config_file(tmp)), "--set", planted, "--to", str(tmp / "got")], s3) == 2
    assert not (tmp / "got").exists()
    assert run(["verify", "--config", str(config_file(tmp)), "--set", genuine], s3) == 0


def test_a_signed_set_stamped_after_its_upload_is_refused(local_set):
    """Even with the signing key (a host-root attacker), a future-stamped plant meant to stay "newest" is refused:
    the provider's upload time is not the uploader's to choose."""
    tmp, genuine = local_set
    s3 = FakeS3()
    assert run(["upload", "--config", str(config_file(tmp)), "--set", genuine], s3) == 0
    planted = _plant(s3, genuine, "20991231T000000Z", None)
    data = s3.objects[("hc-backups-test", f"test/{planted}/COMPLETE.json")][0]
    s3.objects[("hc-backups-test", f"test/{planted}/COMPLETE.json.sig")] = (ssh_sign(signing_keys(tmp)[0], data), {})
    cfg = offsite.read_config(str(config_file(tmp)))
    with pytest.raises(offsite.Refused, match="offsite_set_stamped_after_its_upload"):
        offsite.verify(cfg, planted, s3)
    result = offsite.verify(cfg, genuine, s3)
    assert result["signed_by"] == "hyperchat-offsite-test" and result["uploaded_at"].endswith("Z")


def test_an_unsigned_marker_planted_before_the_upload_is_refused_not_completed(local_set):
    tmp, base = local_set
    s3 = FakeS3()
    s3.objects[("hc-backups-test", f"test/{base}/COMPLETE.json")] = (json.dumps(
        {"format": 1, "environment": "test", "database": "hc_backuptest_a", "set": base, "objects": []}).encode(), {})
    assert run(["upload", "--config", str(config_file(tmp)), "--set", base], s3) == 2 and s3.puts == []


@pytest.mark.parametrize("what", ["key readable by others", "no key", "no allowed signers"])
def test_an_upload_without_a_usable_signing_key_uploads_nothing(local_set, what):
    tmp, base = local_set
    cfg_path = config_file(tmp)
    key, allowed = signing_keys(tmp)
    if what == "key readable by others":
        key.chmod(0o644)
    elif what == "no key":
        key.unlink()
    else:
        allowed.unlink()
    s3 = FakeS3()
    assert run(["upload", "--config", str(cfg_path), "--set", base], s3) == 2 and s3.puts == []


def test_signing_paths_are_fixed_for_production_and_dev(tmp_path):
    cfg = offsite.read_config(str(config_file(tmp_path, env="prod", LOCAL_DIR="/var/backups/hyperchat",
                                              DATABASE="hyperchat", STATE_DIR="/var/lib/hyperchat-offsite/prod")))
    assert str(cfg.signing_key) == "/etc/hyperchat-backup/offsite-sign-prod.key"
    assert str(cfg.allowed_signers) == "/etc/hyperchat-backup/offsite-sign-prod.allowed"
    assert cfg.principal == "hyperchat-offsite-prod"
    with pytest.raises(offsite.Refused, match="config_signing_paths_are_fixed"):
        offsite.read_config(str(config_file(tmp_path, env="dev", LOCAL_DIR="/var/backups/hyperchat-dev",
                                            DATABASE="hyperchat_dev", STATE_DIR="/var/lib/hyperchat-offsite/dev",
                                            SIGNING_KEY_FILE="/tmp/k")))
    with pytest.raises(offsite.Refused, match="config_test_scope"):
        offsite.read_config(str(config_file(tmp_path, SIGNING_KEY_FILE="/etc/hyperchat-backup/offsite-sign-prod.key")))
