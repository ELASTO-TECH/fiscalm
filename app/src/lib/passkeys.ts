/** Native WebAuthn boundary. The server verifies all returned proofs and account binding. */
export type CreationOptions = Omit<PublicKeyCredentialCreationOptions, "challenge" | "user" | "excludeCredentials"> & {
  challenge: string;
  user: Omit<PublicKeyCredentialUserEntity, "id"> & { id: string };
  excludeCredentials: { type: "public-key"; id: string }[];
};
export type AssertionOptions = Omit<PublicKeyCredentialRequestOptions, "challenge" | "allowCredentials"> & { challenge: string };
export type PasskeyChallenge<T> = { ticket: string; publicKey: T };

export function passkeysSupported(): boolean {
  return typeof window !== "undefined" && window.isSecureContext && typeof PublicKeyCredential !== "undefined"
    && typeof navigator.credentials?.create === "function" && typeof navigator.credentials?.get === "function";
}

function decode(value: string): ArrayBuffer {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new TypeError("Invalid base64url");
  const bytes = Uint8Array.from(atob(value.replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0));
  return bytes.buffer;
}

function encode(value: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(value))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function check(signal: AbortSignal): void {
  signal.throwIfAborted();
  if (!passkeysSupported()) throw new DOMException("WebAuthn unavailable", "NotSupportedError");
}

export async function createPasskey(options: CreationOptions, signal: AbortSignal): Promise<Record<string, string>> {
  check(signal);
  const publicKey: PublicKeyCredentialCreationOptions = { ...options, challenge: decode(options.challenge),
    user: { ...options.user, id: decode(options.user.id) },
    excludeCredentials: options.excludeCredentials.map(key => ({ ...key, id: decode(key.id) })) };
  const credential = await navigator.credentials.create({ publicKey, signal });
  // Even a browser that resolves after cancellation must not publish a proof to the next screen.
  signal.throwIfAborted();
  if (!(credential instanceof PublicKeyCredential) || !(credential.response instanceof AuthenticatorAttestationResponse)) {
    throw new TypeError("Invalid registration response");
  }
  return { credential_id: encode(credential.rawId), client_data: encode(credential.response.clientDataJSON),
    attestation: encode(credential.response.attestationObject), transports: JSON.stringify(credential.response.getTransports()) };
}

export async function assertPasskey(options: AssertionOptions, signal: AbortSignal): Promise<Record<string, string>> {
  check(signal);
  const credential = await navigator.credentials.get({ publicKey: { ...options, challenge: decode(options.challenge) }, signal });
  signal.throwIfAborted();
  if (!(credential instanceof PublicKeyCredential) || !(credential.response instanceof AuthenticatorAssertionResponse) || !credential.response.userHandle?.byteLength) {
    throw new TypeError("Invalid assertion response");
  }
  return { credential_id: encode(credential.rawId), user_handle: encode(credential.response.userHandle),
    client_data: encode(credential.response.clientDataJSON), authenticator_data: encode(credential.response.authenticatorData),
    signature: encode(credential.response.signature) };
}

/** A coarse browser-device nickname, never an authenticator identity or OS hostname. */
export function suggestedPasskeyName(client: Pick<Navigator, "userAgent" | "maxTouchPoints">): string {
  const ua = client.userAgent;
  let device = "Dispozitiv";
  if (/iPad/.test(ua) || (/Macintosh/.test(ua) && client.maxTouchPoints > 1)) device = "iPad";
  else if (/iPhone/.test(ua)) device = "iPhone";
  else if (/Android/.test(ua)) device = "Android";
  else if (/Windows/.test(ua)) device = "Windows";
  else if (/Macintosh|Mac OS X/.test(ua)) device = "Mac";
  else if (/Linux/.test(ua)) device = "Linux";
  return `${device} Passkey`;
}

/** The last sign-in method on this device: not a secret, only which form comes first. */
const METHOD = "fiscalm:sign-in-method";
export function rememberedMethod(): "password" | "passkey" | null {
  try { const value = localStorage.getItem(METHOD); return value === "password" || value === "passkey" ? value : null; } catch { return null; }
}
export function rememberMethod(method: "password" | "passkey") {
  try { localStorage.setItem(METHOD, method); } catch { /* The order falls back to passkey first. */ }
}
