// Passkeys (WebAuthn, FR-2) in the browser. The server sends and takes the standard JSON forms,
// and the browser turns them into the binary forms (parse...FromJSON and toJSON), so no base64
// code is needed here. Browsers without these functions fall back to password and TOTP.

type Json = {
  parseCreationOptionsFromJSON(options: unknown): PublicKeyCredentialCreationOptions;
  parseRequestOptionsFromJSON(options: unknown): PublicKeyCredentialRequestOptions;
};

function api(): Json | null {
  if (typeof window === "undefined") return null;
  const pkc = (window as unknown as { PublicKeyCredential?: Partial<Json> }).PublicKeyCredential;
  if (!pkc || !pkc.parseCreationOptionsFromJSON || !pkc.parseRequestOptionsFromJSON) return null;
  return pkc as Json;
}

export const passkeysSupported = () => api() !== null;

const toJson = (credential: Credential | null): unknown => {
  if (!credential) throw new Error("cancelled");
  return (credential as unknown as { toJSON(): unknown }).toJSON();
};

/** Ask the device to make a passkey from the server's options. */
export async function createCredential(options: unknown): Promise<unknown> {
  const parse = api();
  if (!parse) throw new Error("unsupported");
  const publicKey = parse.parseCreationOptionsFromJSON(options);
  return toJson(await navigator.credentials.create({ publicKey }));
}

/** Ask the device to sign in with one of its passkeys. */
export async function getCredential(options: unknown): Promise<unknown> {
  const parse = api();
  if (!parse) throw new Error("unsupported");
  const publicKey = parse.parseRequestOptionsFromJSON(options);
  return toJson(await navigator.credentials.get({ publicKey }));
}
