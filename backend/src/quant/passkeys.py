"""Passkeys (WebAuthn, FR-2): the thin layer over the `webauthn` package.

The package does the cryptography (it checks the challenge, the origin, the relying-party id, the
signature and the signature counter). This module turns its answers into plain dicts and bytes, so
the routes and the tests do not depend on its types, and tests can replace the two `verify_*`
functions (no test can hold a real authenticator).

A passkey sign-in asks for user verification (a fingerprint, a face or a device PIN), so it counts
as two factors on its own: no TOTP code is asked after it.
"""

import json
from dataclasses import dataclass
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from quant.config import get_settings
from quant.models import User


class PasskeyError(Exception):
    """The browser's answer did not verify."""


@dataclass(frozen=True)
class NewCredential:
    credential_id: bytes
    public_key: bytes
    sign_count: int


def registration_options(user: User, existing: list[bytes]) -> tuple[dict[str, Any], bytes]:
    """Options for the browser's `navigator.credentials.create`, and the challenge to remember.
    A discoverable credential is asked for, so the sign-in needs no email. Passkeys the user has
    already are excluded: one device does not register twice."""
    settings = get_settings()
    options = generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=user.id.bytes,
        user_name=user.email,
        user_display_name=user.display_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=c) for c in existing],
    )
    parsed: dict[str, Any] = json.loads(options_to_json(options))
    return parsed, options.challenge


def verify_registration(credential: dict[str, Any], challenge: bytes) -> NewCredential:
    settings = get_settings()
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 - the package has many error types; all mean "no"
        raise PasskeyError(type(exc).__name__) from exc
    return NewCredential(
        verified.credential_id, verified.credential_public_key, verified.sign_count
    )


def login_options() -> tuple[dict[str, Any], bytes]:
    """Options for `navigator.credentials.get` without a list of credentials: the device offers
    the passkeys it holds for this site."""
    settings = get_settings()
    options = generate_authentication_options(
        rp_id=settings.webauthn_rp_id,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    parsed: dict[str, Any] = json.loads(options_to_json(options))
    return parsed, options.challenge


def verify_login(
    credential: dict[str, Any], challenge: bytes, public_key: bytes, sign_count: int
) -> int:
    """Check a sign-in. Returns the new signature counter."""
    settings = get_settings()
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_origin,
            credential_public_key=public_key,
            credential_current_sign_count=sign_count,
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 - see above
        raise PasskeyError(type(exc).__name__) from exc
    return int(verified.new_sign_count)
