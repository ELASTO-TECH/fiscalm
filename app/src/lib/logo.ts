/** PNG or JPEG by the file's first bytes; null for anything else. */
export async function imageKind(f: File): Promise<"image/png" | "image/jpeg" | null> {
  const b = new Uint8Array(await f.slice(0, 8).arrayBuffer());
  if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return "image/png";
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return "image/jpeg";
  return null;
}
