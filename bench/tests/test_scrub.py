"""Scrub-gate tests (§5.1, s2(a)).

Covers:
- the scrub pass removes a planted fake sk- key, a fake email, and maps a fake
  real-name to the Infektyd alias;
- the cryptographic cross-check: verify_scrubbed() ACCEPTS a properly scrubbed
  snapshot and REJECTS an un-scrubbed-but-flagged corpus (a bare scrubbed=True
  over unscrubbed/tampered bytes), and rejects scrubbed=True with no hash.
"""

import json

import pytest

from membench.scrub import (
    DEFAULT_NAME_ALIAS,
    REDACTED_EMAIL,
    REDACTED_JWT,
    REDACTED_KEY,
    REDACTED_PEM,
    REDACTED_SECRET,
    ScrubGateError,
    ScrubPolicy,
    compute_scrub_manifest_hash,
    default_policy,
    residual_secrets,
    scrub_text,
    scrub_snapshot,
    verify_scrubbed,
)
from membench.corpus import compute_content_hash
from membench.snapshot import (
    MANIFEST_FILENAME,
    corpus_subdir,
    freeze_snapshot,
    load_manifest,
)

FAKE_KEY = "sk-TESTKEY0123456789abcdefABCDEF"
FAKE_EMAIL = "testy.mcfakeface@example.com"
FAKE_NAME = "Testy McFakeface"

PLANTED = (
    "# Secrets Doc\n\n"
    f"My API key is {FAKE_KEY} do not share.\n"
    f"Contact {FAKE_EMAIL} for access.\n"
    f"This note was written by {FAKE_NAME} on a Tuesday.\n"
)


def _policy() -> ScrubPolicy:
    return default_policy(operator_real_name=FAKE_NAME)


def test_scrub_text_redacts_key_email_name():
    scrubbed, spans = scrub_text("x.md", PLANTED, _policy())
    assert FAKE_KEY not in scrubbed
    assert FAKE_EMAIL not in scrubbed
    assert FAKE_NAME not in scrubbed
    assert REDACTED_KEY in scrubbed
    assert REDACTED_EMAIL in scrubbed
    assert DEFAULT_NAME_ALIAS in scrubbed
    kinds = {s.kind for s in spans}
    assert kinds == {"key", "email", "name"}


# ── Fix 6: new secret classes (all FAKE values, fake-fixture convention) ─────
# A FAKE PEM private key (clearly bogus body), a FAKE JWT, fake credential
# assignments, and a FAKE Anthropic sk-ant-api03- key.
FAKE_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Q\n"
    "uKUpRKfFLfRYC9AIKjbJTWit+CqvjKkfakefakefakefakefakefakefakefake==\n"
    "-----END RSA PRIVATE KEY-----"
)
FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkZha2UifQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
FAKE_ANT_KEY = "sk-ant-api03-FAKEFAKEfakefake0123456789abcdefABCDEF_-deadbeef00"


def test_scrub_text_redacts_pem_private_key():
    text = f"Here is a deploy key:\n{FAKE_PEM}\nuse it.\n"
    scrubbed, spans = scrub_text("k.md", text, ScrubPolicy())
    assert "PRIVATE KEY" not in scrubbed or REDACTED_PEM in scrubbed
    assert "MIIBOgIBAAJBAKj34" not in scrubbed  # the key body is gone
    assert REDACTED_PEM in scrubbed
    # Surrounding prose is preserved (no over-match).
    assert scrubbed.startswith("Here is a deploy key:")
    assert scrubbed.rstrip().endswith("use it.")
    assert {s.kind for s in spans} == {"pem"}


def test_scrub_text_redacts_jwt():
    text = f"token header.payload below: {FAKE_JWT} end"
    scrubbed, spans = scrub_text("j.md", text, ScrubPolicy())
    assert FAKE_JWT not in scrubbed
    assert REDACTED_JWT in scrubbed
    assert "jwt" in {s.kind for s in spans}


def test_scrub_text_redacts_credential_assignments():
    text = (
        'password="hunter2secret"\n'
        "secret=supersecretvalue123\n"
        "api_key: abcdef0123456789\n"
        "token=tok_FAKE_aaaaaaaaaaaa\n"
    )
    scrubbed, spans = scrub_text("c.md", text, ScrubPolicy())
    for plaintext in ("hunter2secret", "supersecretvalue123",
                      "abcdef0123456789", "tok_FAKE_aaaaaaaaaaaa"):
        assert plaintext not in scrubbed, plaintext
    assert REDACTED_SECRET in scrubbed
    # The KEY names survive — only the value is redacted (no prose corruption).
    assert "password=" in scrubbed
    assert "secret=" in scrubbed
    assert "api_key:" in scrubbed
    assert "token=" in scrubbed
    assert "secret" in {s.kind for s in spans}
    # Fix 8: PIN the delimiter for the QUOTED case so a regex change that drops
    # the closing quote is caught. The quoted value was double-quoted, so the
    # redacted form must re-emit BOTH quotes around the sentinel — a stray
    # unbalanced quote (e.g. password="[REDACTED_SECRET]\n) would fail here.
    assert f'password="{REDACTED_SECRET}"' in scrubbed
    # And the UNQUOTED cases must NOT have spurious quotes added around them.
    assert f"secret={REDACTED_SECRET}" in scrubbed
    assert f"token={REDACTED_SECRET}" in scrubbed


def test_scrub_text_redacts_quoted_multiword_credential(review_fix=True):
    # Review fix 3: a QUOTED multi-word passphrase must be fully redacted. The old
    # value pattern stopped at the first space, leaving "horse battery staple"
    # plaintext — a scrub-gate FALSE NEGATIVE (verify_scrubbed re-scans with the
    # same pattern). The quoted branch now spans the whole quoted value.
    text = 'password="correct horse battery staple"\n'
    scrubbed, spans = scrub_text("mw.md", text, ScrubPolicy())
    assert "correct horse battery staple" not in scrubbed
    assert "horse battery staple" not in scrubbed
    assert REDACTED_SECRET in scrubbed
    assert "password=" in scrubbed  # key name + separator survive
    assert "secret" in {s.kind for s in spans}
    # Single-quoted multi-word value too.
    s2, _ = scrub_text("mw2.md", "secret='multi word secret value here'\n", ScrubPolicy())
    assert "multi word secret value here" not in s2
    assert REDACTED_SECRET in s2
    # And it survives a re-scan (idempotent: the sentinel is not re-flagged).
    rescrub, residual = scrub_text("mw.md", scrubbed, ScrubPolicy())
    assert rescrub == scrubbed and residual == []


def test_scrub_text_redacts_unclosed_quote_credential():
    # Review fix: a value that OPENS a quote but never closes it (a missing
    # delimiter in a shell script or a concatenated config fragment) was matched
    # by NEITHER branch: the quoted branch needs a closing quote, the unquoted
    # branch excludes the leading quote char. The value survived scrubbing AND
    # verify_scrubbed re-scanned with the same pattern, so the residual was
    # invisible. The leading-quote-no-close branch now catches it.
    text = 'password="noclosevalue rest of line\n'
    scrubbed, spans = scrub_text("uq.md", text, ScrubPolicy())
    assert "noclosevalue" not in scrubbed
    assert REDACTED_SECRET in scrubbed
    assert "password=" in scrubbed  # key + separator survive
    assert "secret" in {s.kind for s in spans}
    # The trailing prose after the (unclosed) value is left intact.
    assert "rest of line" in scrubbed
    # Idempotent: the sentinel is not re-flagged on a re-scan.
    rescrub, residual = scrub_text("uq.md", scrubbed, ScrubPolicy())
    assert rescrub == scrubbed and residual == []


def test_scrub_text_redacts_pem_as_assignment_value():
    # Fix 2 (security): a PEM private-key block planted as the VALUE of an
    # assignment. The _ASSIGNMENT_PATTERN match starts EARLIER (at "password=")
    # and its unquoted value regex [^\s'"]{6,} stops at the first whitespace, so a
    # start-first overlap resolution would redact only "-----BEGIN" and leave the
    # key BODY in the clear. Longest-match overlap resolution must let the PEM
    # span win so the ENTIRE block (body + END line) is redacted.
    text = f"password={FAKE_PEM}\nrest of config\n"
    scrubbed, spans = scrub_text("pemval.md", text, ScrubPolicy())
    # The key body and the END armor line must both be gone.
    assert "MIIBOgIBAAJBAKj34" not in scrubbed
    assert "PRIVATE KEY" not in scrubbed
    assert "-----END" not in scrubbed
    assert REDACTED_PEM in scrubbed
    # The PEM span (not a truncated 'secret' span) is what fired.
    assert "pem" in {s.kind for s in spans}
    # Surrounding prose survives.
    assert "rest of config" in scrubbed


def test_scrub_text_redacts_five_char_closed_quote():
    # Fix 4: a properly-closed quoted value of exactly 5 chars. The closed-quote
    # branch must match it (consuming its closing quote) rather than the
    # unclosed-quote branch leaving a stray closing quote as literal text.
    text = "password='abcde'\n"
    scrubbed, spans = scrub_text("five.md", text, ScrubPolicy())
    assert "abcde" not in scrubbed
    assert "secret" in {s.kind for s in spans}
    # The closing quote is CONSUMED — the redacted form re-emits both quotes and
    # leaves NO stray trailing quote before the newline.
    assert f"password='{REDACTED_SECRET}'\n" in scrubbed
    assert "''" not in scrubbed  # no doubled/stray quote


def test_quoted_credential_residual_caught_by_verify(tmp_path):
    # End-to-end: a quoted multi-word password planted in a snapshot must be a
    # residual the scrub gate catches if it ever survived — proves the §5.1 gate
    # (residual_secrets / verify_scrubbed) now sees quoted spans. Here we confirm
    # scrub removes it so the gate ACCEPTS the genuinely-scrubbed snapshot.
    from membench.scrub import default_policy, scrub_snapshot, verify_scrubbed

    src = tmp_path / "src"
    src.mkdir()
    (src / "01.md").write_text(
        'config: password="correct horse battery staple"\n', encoding="utf-8"
    )
    dest = tmp_path / "snap"
    freeze_snapshot(src, dest, allow_public=True)
    policy = default_policy()
    scrub_snapshot(dest, policy, allow_public=True)
    # The scrubbed bytes no longer carry the passphrase, and the gate accepts.
    body = (dest / "corpus" / "01.md").read_text(encoding="utf-8")
    assert "horse battery staple" not in body
    verify_scrubbed(dest, policy)


def test_scrub_text_redacts_anthropic_sk_ant_key():
    # Confirm the existing sk- pattern covers sk-ant-api03- (fix 6 verification).
    text = f"ANTHROPIC_API_KEY env holds {FAKE_ANT_KEY} for the run."
    scrubbed, spans = scrub_text("a.md", text, ScrubPolicy())
    assert FAKE_ANT_KEY not in scrubbed
    assert REDACTED_KEY in scrubbed
    assert "key" in {s.kind for s in spans}


def test_assignment_does_not_overmatch_short_or_placeholder_values():
    # A boolean-ish or short value must NOT be redacted (avoids corpus corruption).
    text = "token: true\npassword=\nsecret=ab\n"
    scrubbed, _ = scrub_text("p.md", text, ScrubPolicy())
    assert scrubbed == text  # nothing matched -> bytes unchanged


def test_new_patterns_are_idempotent_under_rescan():
    # A genuinely scrubbed tree must yield ZERO residual hits on re-scan, so the
    # sentinels themselves must not re-match (else verify_scrubbed would fail).
    text = (
        f"key:\n{FAKE_PEM}\n"
        f"jwt {FAKE_JWT}\n"
        'password="hunter2secret"\n'
    )
    scrubbed, _ = scrub_text("m.md", text, ScrubPolicy())
    rescrubbed, spans = scrub_text("m.md", scrubbed, ScrubPolicy())
    assert rescrubbed == scrubbed  # no further redaction
    assert spans == []  # no residual secret detected in the scrubbed bytes


def _make_snapshot(tmp_path, extra_text=PLANTED):
    src = tmp_path / "src"
    src.mkdir()
    (src / "01-secrets.md").write_text(extra_text, encoding="utf-8")
    (src / "02-clean.md").write_text("# Clean\n\nNothing here.\n", encoding="utf-8")
    dest = tmp_path / "snap"
    freeze_snapshot(src, dest, allow_public=True)
    return dest


def test_scrub_snapshot_then_verify_accepts(tmp_path):
    dest = _make_snapshot(tmp_path)
    scrub_hash = scrub_snapshot(dest, _policy(), allow_public=True)
    # The on-disk scrubbed file no longer contains the planted secrets.
    body = (corpus_subdir(dest) / "01-secrets.md").read_text(encoding="utf-8")
    assert FAKE_KEY not in body and FAKE_EMAIL not in body and FAKE_NAME not in body
    # Manifest records scrubbed=True + the hash; the gate ACCEPTS.
    manifest = load_manifest(dest)
    assert manifest.scrubbed is True
    assert manifest.scrub_manifest_hash == scrub_hash
    # Pass the real policy so the name dimension of the residual re-scan is
    # active end-to-end (not just keys + emails).
    verify_scrubbed(dest, policy=_policy())  # must not raise


def test_verify_rejects_unscrubbed_but_flagged(tmp_path):
    """A bare scrubbed=True over UNSCRUBBED bytes is rejected (§5.1).

    The strongest forgery: flip scrubbed=True AND compute a scrub_manifest_hash
    over an EMPTY span list (claiming "scrubbed, nothing to redact") so the hash
    recheck would MATCH. The planted secrets are still physically in the bytes.
    The independent denylist re-scan catches this — the hash alone cannot.
    """
    dest = _make_snapshot(tmp_path)  # NOT scrubbed — secrets still present
    forged_hash = compute_scrub_manifest_hash(corpus_subdir(dest), spans=[])
    manifest_path = dest / MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["scrubbed"] = True
    payload["scrub_manifest_hash"] = forged_hash  # would pass the hash recheck
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    # Precondition: the secret is genuinely still on disk (forgery scrubbed
    # nothing). No scrub_spans.jsonl exists -> empty-span recompute MATCHES the
    # forged hash, so ONLY the residual re-scan stands between this and a pass.
    secret_body = (corpus_subdir(dest) / "01-secrets.md").read_text(encoding="utf-8")
    assert FAKE_KEY in secret_body

    with pytest.raises(ScrubGateError):
        verify_scrubbed(dest, policy=_policy())


def test_verify_rejects_name_only_forgery_with_default_policy(tmp_path):
    """A name-only-PII forgery is caught even with NO policy passed (items 1/5).

    The corpus's ONLY PII is the operator's real name (no keys, no emails). An
    attacker computes scrub_manifest_hash over the UNSCRUBBED tree with an empty
    span list, sets scrubbed=True, and leaves NO scrub_spans.jsonl. The default
    verify_scrubbed(dest) (no policy) must STILL reject: it reconstructs the name
    dimension from the private name sidecar, OR refuses outright when it
    cannot prove the name dimension was considered.
    """
    name_only = (
        "# Notes\n\n"
        f"This whole note was authored by {FAKE_NAME}.\n"
        f"{FAKE_NAME} reviewed it again later.\n"
    )
    dest = _make_snapshot(tmp_path, extra_text=name_only)

    # Forge: scrubbed=True, hash over empty spans, no scrub_spans.jsonl, and no
    # name hashes recorded (the attacker never ran a name-aware scrub).
    forged_hash = compute_scrub_manifest_hash(corpus_subdir(dest), spans=[])
    manifest_path = dest / MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["scrubbed"] = True
    payload["scrub_manifest_hash"] = forged_hash
    payload["name_alias_key_hashes"] = []  # forger recorded none
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    body = (corpus_subdir(dest) / "01-secrets.md").read_text(encoding="utf-8")
    assert FAKE_NAME in body  # the name really is still on disk

    # Default call (NO policy) must reject — either via the no-spans/no-keys
    # refusal or the residual name re-scan if keys were reconstructed.
    with pytest.raises(ScrubGateError):
        verify_scrubbed(dest)


def test_verify_name_only_caught_via_persisted_keys(tmp_path):
    """A properly-scrubbed name-only corpus persists the real names in the PRIVATE
    sidecar, so a LATER name-residual (re-introduced byte) is caught by the
    default-policy re-scan (items 2/6 — both the PASS and the CATCH path).

    The manifest itself must NOT carry the plaintext name (item 3); the verifier
    recovers the name only from scrub_spans/_private (gitignored).
    """
    name_only = f"# Notes\n\nAuthored by {FAKE_NAME}.\n"
    dest = _make_snapshot(tmp_path, extra_text=name_only)
    scrub_snapshot(dest, _policy(), allow_public=True)

    # PASS path: the clean scrubbed state verifies with the default (no-policy)
    # call, reconstructing the name dimension from the private sidecar.
    manifest = load_manifest(dest)
    assert FAKE_NAME not in manifest.name_alias_key_hashes  # no plaintext in manifest
    assert manifest.name_alias_key_hashes  # but the salted-hash signal is present
    verify_scrubbed(dest)  # clean, default policy, name dimension active

    # CATCH path: re-introduce the FAKE real name byte into a scrubbed file AFTER
    # the scrub, then RE-FORGE both the content_hash and scrub_manifest_hash so
    # they recompute over the tampered tree — defeating the hash/content gates.
    # The ONLY defense left is the persisted-private-name re-scan, which must
    # DETECT the residual name and raise.
    victim = corpus_subdir(dest) / "01-secrets.md"
    victim.write_text(
        victim.read_text(encoding="utf-8") + f"\nPS: {FAKE_NAME} again.\n",
        encoding="utf-8",
    )
    from membench.scrub import _load_spans  # recompute hashes over the new bytes

    spans = _load_spans(dest)
    new_content = compute_content_hash(corpus_subdir(dest))
    new_scrub_hash = compute_scrub_manifest_hash(corpus_subdir(dest), spans)
    manifest_path = dest / MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["content_hash"] = new_content
    payload["scrub_manifest_hash"] = new_scrub_hash
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ScrubGateError) as exc:
        verify_scrubbed(dest)  # default policy, name dimension from private keys
    assert "re-scan" in str(exc.value)  # caught by the residual re-scan, not a hash


def test_load_spans_rejects_malformed_json_line(tmp_path):
    # Fix 3: a malformed-JSON line in scrub_spans.jsonl must raise ScrubGateError
    # (caught by corpus._enforce_scrub_gate), NOT a raw json.JSONDecodeError that
    # escapes the gate. json.loads is now INSIDE the try.
    from membench.scrub import _load_spans

    dest = _make_snapshot(tmp_path)
    (dest / "scrub_spans.jsonl").write_text(
        "{not valid json at all\n", encoding="utf-8"
    )
    with pytest.raises(ScrubGateError) as exc:
        _load_spans(dest)
    assert "malformed scrub span" in str(exc.value)
    # The raw line bytes must NOT be echoed (redaction).
    assert "not valid json at all" not in str(exc.value)


def test_load_spans_rejects_non_dict_line(tmp_path):
    # Fix 3: a VALID-but-non-dict JSON line (e.g. a JSON array) makes d["doc_id"]
    # raise TypeError, which the bare `except KeyError` missed and which escaped
    # the gate as a raw TypeError. It must now raise ScrubGateError.
    from membench.scrub import _load_spans

    dest = _make_snapshot(tmp_path)
    (dest / "scrub_spans.jsonl").write_text(
        '["doc", "kind", 0, 5, "x"]\n', encoding="utf-8"
    )
    with pytest.raises(ScrubGateError) as exc:
        _load_spans(dest)
    assert "malformed scrub span" in str(exc.value)


def test_verify_rejects_scrubbed_true_without_hash(tmp_path):
    dest = _make_snapshot(tmp_path)
    manifest_path = dest / MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["scrubbed"] = True
    payload["scrub_manifest_hash"] = ""  # bare boolean, no hash
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ScrubGateError):
        verify_scrubbed(dest)


def test_verify_rejects_unscrubbed_manifest(tmp_path):
    """A freshly-frozen (scrubbed=False) snapshot is rejected by the gate."""
    dest = _make_snapshot(tmp_path)
    with pytest.raises(ScrubGateError):
        verify_scrubbed(dest)


def test_scrub_snapshot_refuses_off_private(tmp_path):
    """scrub_snapshot is an in-place writer; off-private without allow_public
    must raise the private-path guard (task item 5 / §5.1)."""
    from membench.paths import PrivatePathError

    src = tmp_path / "src"
    src.mkdir()
    (src / "01.md").write_text(PLANTED, encoding="utf-8")
    dest = tmp_path / "snap"  # tmp_path is NOT under PRIVATE_ROOT
    freeze_snapshot(src, dest, allow_public=True)
    with pytest.raises(PrivatePathError):
        scrub_snapshot(dest, _policy())  # allow_public defaults False


def test_load_corpus_scrubbed_true_accepts_gated_snapshot(tmp_path):
    """load_corpus(scrubbed=True) ACCEPTS a properly freeze+scrub-gated snapshot
    (the production path used by label_cli draft/validate — item 6)."""
    from membench.corpus import load_corpus

    dest = _make_snapshot(tmp_path)
    scrub_snapshot(dest, _policy(), allow_public=True)
    manifest = load_manifest(dest)
    corpus = load_corpus(
        corpus_subdir(dest),
        pinned_hash=manifest.content_hash,
        scrubbed=True,
        snapshot_dir=dest,
    )
    assert corpus.content_hash == manifest.content_hash
    assert corpus.scrubbed is True


def test_load_corpus_scrubbed_true_rejects_unscrubbed_snapshot(tmp_path):
    """load_corpus(scrubbed=True) over a freshly-frozen (un-scrubbed) snapshot
    raises CorpusPathError — the scrub gate refuses to honor the flag (item 6)."""
    from membench.corpus import CorpusPathError, load_corpus

    dest = _make_snapshot(tmp_path)  # frozen, NOT scrubbed -> manifest scrubbed=False
    manifest = load_manifest(dest)
    with pytest.raises(CorpusPathError):
        load_corpus(
            corpus_subdir(dest),
            pinned_hash=manifest.content_hash,
            scrubbed=True,
            snapshot_dir=dest,
        )


def test_load_corpus_scrubbed_rejects_diverged_corpus_dir(tmp_path):
    """load_corpus(scrubbed=True) with corpus_dir != snapshot/corpus/ is refused
    BEFORE the gate runs — else the gate would verify a different tree than the
    one served, letting unscrubbed bytes pass (item 3)."""
    from membench.corpus import CorpusPathError, load_corpus

    # A properly scrubbed snapshot (the gate would pass over IT)...
    gated_root = tmp_path / "gated"
    gated_root.mkdir()
    gated = _make_snapshot(gated_root)
    scrub_snapshot(gated, _policy(), allow_public=True)

    # ...but serve a DIFFERENT, unscrubbed corpus whose content-hash we pin.
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = _make_snapshot(other_root)  # unscrubbed real-ish data
    from membench.corpus import compute_content_hash

    served = corpus_subdir(other)
    served_hash = compute_content_hash(served)

    with pytest.raises(CorpusPathError):
        load_corpus(
            served,  # served corpus
            pinned_hash=served_hash,
            scrubbed=True,
            snapshot_dir=gated,  # gate would verify gated/corpus, not served
        )


def test_verify_rejects_opted_out_names_without_policy(tmp_path):
    """A scrub that opted out of name redaction records name_scrub_opted_out=True;
    verify_scrubbed with NO policy must REFUSE (cannot re-scan for unknown names),
    even though key/email spans exist (item 1 — the else-branch fall-through)."""
    # Corpus has a key (gets scrubbed) AND a real name (NOT scrubbed: empty
    # name_aliases). So scrub_spans.jsonl is non-empty after the key scrub.
    text = (
        "# Doc\n\n"
        f"key {FAKE_KEY} here.\n"
        f"authored by {FAKE_NAME}.\n"
    )
    dest = _make_snapshot(tmp_path, extra_text=text)
    # Policy redacts keys/emails but has NO name_aliases -> opt-out recorded.
    scrub_snapshot(dest, ScrubPolicy(name_aliases={}), allow_public=True)
    manifest = load_manifest(dest)
    assert manifest.name_scrub_opted_out is True
    assert manifest.name_alias_key_hashes == ()
    # The real name is still physically on disk (never scrubbed).
    body = (corpus_subdir(dest) / "01-secrets.md").read_text(encoding="utf-8")
    assert FAKE_NAME in body
    # No-policy verify must REFUSE rather than fall through to a name-blind
    # ScrubPolicy() (which would silently pass the residual name).
    with pytest.raises(ScrubGateError):
        verify_scrubbed(dest)
    # With the real policy, the residual name IS caught by the re-scan.
    with pytest.raises(ScrubGateError):
        verify_scrubbed(dest, policy=_policy())


def test_verify_detects_post_scrub_tamper(tmp_path):
    """Scrub correctly, then tamper a byte -> hash mismatch -> reject."""
    dest = _make_snapshot(tmp_path)
    scrub_snapshot(dest, _policy(), allow_public=True)
    verify_scrubbed(dest)  # clean
    victim = corpus_subdir(dest) / "02-clean.md"
    victim.write_text(victim.read_text() + "X", encoding="utf-8")
    with pytest.raises(ScrubGateError):
        verify_scrubbed(dest)


# A FAKE planted real name, deliberately NOT the operator's — used to prove no
# plaintext real name reaches the (potentially public) manifest.json (item 3).
PLANTED_FAKE_NAME = "Testy McFakeface"


def test_manifest_never_contains_plaintext_name_when_public(tmp_path):
    """With allow_public=True, NO plaintext real name appears in manifest.json
    (item 3, §5.1). The manifest may carry only SALTED hashes; the plaintext
    real names live solely in the private scrub_spans/_private sidecar."""
    text = f"# Notes\n\nAuthored by {PLANTED_FAKE_NAME} on a Tuesday.\n"
    dest = _make_snapshot(tmp_path, extra_text=text)
    scrub_snapshot(
        dest,
        default_policy(operator_real_name=PLANTED_FAKE_NAME),
        allow_public=True,
    )

    manifest_bytes = (dest / MANIFEST_FILENAME).read_text(encoding="utf-8")
    # The plaintext name must NOT be anywhere in the manifest (full or any token).
    assert PLANTED_FAKE_NAME not in manifest_bytes
    assert "Testy" not in manifest_bytes
    assert "McFakeface" not in manifest_bytes
    # The corpus bytes must also be scrubbed of it.
    body = (corpus_subdir(dest) / "01-secrets.md").read_text(encoding="utf-8")
    assert PLANTED_FAKE_NAME not in body
    # The salted-hash signal IS recorded (proving names were considered)...
    manifest = load_manifest(dest)
    assert manifest.name_alias_key_hashes
    # ...and the plaintext lives ONLY in the private, gitignored sidecar.
    from membench.scrub import _name_keys_path

    sidecar = _name_keys_path(dest)
    assert sidecar.exists()
    assert PLANTED_FAKE_NAME in sidecar.read_text(encoding="utf-8")
    # The private sidecar sits under a `_private/` segment (matched by the repo
    # `**/_private/` ignore rule), so it can never be staged from a public path.
    assert "_private" in sidecar.parts


def test_load_spans_malformed_raises_scrub_gate_error(tmp_path):
    """A malformed scrub_spans.jsonl line (missing field) raises ScrubGateError,
    NOT a bare KeyError that would crash before the gate reports (item 4)."""
    from membench.scrub import _load_spans

    dest = _make_snapshot(tmp_path)
    scrub_snapshot(dest, _policy(), allow_public=True)
    spans_path = dest / "scrub_spans.jsonl"
    # Overwrite with a line missing the 'replacement' field.
    spans_path.write_text(
        json.dumps({"doc_id": "01-secrets.md", "kind": "key", "start": 0, "end": 5})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ScrubGateError) as exc:
        _load_spans(dest)
    assert "malformed scrub span" in str(exc.value)
    assert "replacement" in str(exc.value)
    # And the full gate surfaces it as a ScrubGateError too (not a KeyError).
    with pytest.raises(ScrubGateError):
        verify_scrubbed(dest, policy=_policy())


@pytest.mark.parametrize("bad_names", [None, "Hans", {}])
def test_load_private_name_keys_non_list_yields_empty(tmp_path, bad_names):
    """A tampered name_keys.json with a non-list ``names`` (null / str / dict)
    yields [] — NO uncaught TypeError, and crucially NO per-character iteration
    of a bare string (items 1 & 2). The string case is the security-critical one:
    iterating ``"Hans"`` would emit single-letter \\b patterns that false-match
    the whole corpus, so we assert the result is empty (no patterns at all)."""
    from membench.scrub import _load_private_name_keys, _name_keys_path

    dest = _make_snapshot(tmp_path)
    scrub_snapshot(dest, _policy(), allow_public=True)
    sidecar = _name_keys_path(dest)
    sidecar.write_text(json.dumps({"names": bad_names}), encoding="utf-8")

    keys = _load_private_name_keys(dest)  # must NOT raise TypeError
    assert keys == []
    # Belt-and-braces for item 2: no single-character key leaked from a string.
    assert all(len(k) != 1 for k in keys)


def test_load_spans_non_int_offset_raises_scrub_gate_error(tmp_path):
    """A scrub_spans.jsonl span with a non-int start/end raises ScrubGateError,
    not a downstream offset-arithmetic crash (item 3). The fields are annotated
    int but the JSONL is edit-controlled, so the type is enforced at load."""
    from membench.scrub import _load_spans

    dest = _make_snapshot(tmp_path)
    scrub_snapshot(dest, _policy(), allow_public=True)
    spans_path = dest / "scrub_spans.jsonl"
    spans_path.write_text(
        json.dumps(
            {
                "doc_id": "01-secrets.md",
                "kind": "key",
                "start": "not_an_int",
                "end": 5,
                "replacement": "[REDACTED]",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ScrubGateError) as exc:
        _load_spans(dest)
    assert "start/end must be int" in str(exc.value)
