/**
 * Validation messages that can be translated.
 *
 * `21-localization.md` lists **localized validation messages** among the things
 * the interface must produce, and Zod is where the platform's validation lives.
 * The awkward part is *when*: a schema is a module-level constant built once at
 * import, long before React exists and therefore long before anybody's language
 * preference has been read. A schema cannot call `useTranslations`, and a schema
 * rebuilt per render would break React Hook Form's resolver identity.
 *
 * **So a schema emits a key and a component renders it.** `vm("case.titleRequired")`
 * produces an opaque marker string that travels through Zod, through React Hook
 * Form's `errors.title.message`, and out to `hooks/use-field-error.ts`, which
 * decodes it and looks it up in the reader's catalogue. The message never appears
 * on screen in this form.
 *
 * **Values are carried with the key, not baked into it.** *"Title must be at most
 * 255 characters"* has a number in it, and the number is decided by the schema
 * while the sentence is decided by the translator — so `vm("maxLength", { max: 255 })`
 * keeps both halves where they belong and lets Arabic put the number wherever
 * Arabic puts it.
 *
 * **A plain string still works.** Anything this cannot decode is passed through
 * unchanged, which is what keeps a server-supplied field message, a Zod built-in,
 * and a third-party refinement rendering rather than throwing.
 */

/** Marks a string as "a translation key, not a sentence". */
const MARKER = "i18n:";

/** Values interpolated into a validation message. */
export type ValidationValues = Record<string, string | number>;

/**
 * A translatable validation message.
 *
 * The key is **fully qualified** against the catalogue root — `validation.case.title`
 * — because a schema is imported by several forms and has no namespace of its own.
 */
export function vm(key: string, values?: ValidationValues): string {
  return values && Object.keys(values).length > 0
    ? `${MARKER}${key}:${JSON.stringify(values)}`
    : `${MARKER}${key}`;
}

/** A decoded validation message, or `null` when the input was a plain sentence. */
export interface DecodedValidationMessage {
  key: string;
  values?: ValidationValues;
}

/**
 * Decode a message produced by {@link vm}.
 *
 * Returns `null` for anything else — a server message, a Zod default, a literal —
 * so a caller can fall back to displaying it verbatim rather than losing it.
 */
export function decodeValidationMessage(
  message: string | undefined | null,
): DecodedValidationMessage | null {
  if (!message || !message.startsWith(MARKER)) return null;

  const rest = message.slice(MARKER.length);
  const split = rest.indexOf(":");
  if (split === -1) return { key: rest };

  const key = rest.slice(0, split);
  try {
    const values = JSON.parse(rest.slice(split + 1)) as ValidationValues;
    return { key, values };
  } catch {
    // A malformed payload is a bug in a schema, not a reason to fail a render:
    // the key alone still resolves to a sentence.
    return { key };
  }
}
