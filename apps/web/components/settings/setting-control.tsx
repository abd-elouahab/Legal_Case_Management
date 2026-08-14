"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { languageValueLabel } from "@/components/settings/labels";

/**
 * The words for a permitted value, in the reader's language.
 *
 * One hook rather than two lookups at every call site, because a choice list can
 * hold three different kinds of identifier: a **language** (never translated —
 * see `languageValueLabel`), a **widget key** (its own namespace, shared with the
 * dashboard's picker), and everything else. A value none of the three names
 * resolves through the provider's fallback to a humanized form of itself, which
 * is what lets a choice the server added appear before anybody translates it.
 */
function useValueLabel(): (value: string) => string {
  const tValues = useTranslations("settings.values");
  const tWidgets = useTranslations("settings.widgets");

  return React.useCallback(
    (value: string) => {
      const language = languageValueLabel(value);
      if (language) return language;

      // `t.has` rather than `??`: a missing key does not return `undefined` — it
      // returns the provider's humanized fallback, so chaining with `??` would
      // never reach the widget namespace and every widget would render as
      // "Recent Cases" instead of "Recent cases".
      if (tValues.has(value)) return tValues(value);
      return tWidgets(value);
    },
    [tValues, tWidgets],
  );
}
import type { SettingDefinition, SettingValue } from "@/types/settings";

/**
 * One setting's control, chosen from the definition the API served.
 *
 * **A renderer per value type, never one per setting.** There are five value
 * types and ten user settings today; a component per setting would be ten
 * components and an eleventh for every setting added — which is exactly the
 * redesign-on-extension `20-settings.md` rules out. This is the same trade
 * `19-dashboard-analytics.md` made with nine payload kinds for nineteen widgets,
 * and it is what lets a setting added on the server render in a browser nobody
 * redeployed.
 *
 * The **words** are this file's; the **vocabulary** is the server's. `choices`
 * arrives as a list of stable identifiers and `labels.ts` turns each into a
 * sentence, falling back to the identifier — so an unlabelled value is a cosmetic
 * gap rather than an empty dropdown.
 *
 * **Every control saves on change**, and there is deliberately no Save button.
 * A preference is one value; a form that batched them would let somebody close
 * the page believing they had switched something. The exception is free text,
 * where saving per keystroke would be one request per character — those commit on
 * blur, which is the first moment somebody has finished typing. The failure path
 * is the owner's: it restores the previous value and says so, which is what makes
 * an immediate save safe.
 */

export interface SettingControlProps {
  definition: SettingDefinition;
  value: SettingValue;
  /** Whether this is the platform's answer rather than one somebody chose. */
  isDefault: boolean;
  disabled?: boolean;
  onChange: (value: SettingValue) => void;
  /** Prefix for input ids, so the same setting can appear in two panels. */
  idPrefix?: string;
}

export function SettingControl({
  definition,
  value,
  isDefault,
  disabled = false,
  onChange,
  idPrefix = "setting",
}: SettingControlProps) {
  const t = useTranslations("settings.definitions");
  const valueLabel = useValueLabel();
  const title = t(`${definition.key}.title`);
  const description = t(`${definition.key}.description`);
  const id = `${idPrefix}-${definition.key}`;
  const descriptionId = `${id}-description`;

  // A boolean reads as a row with the switch beside its label rather than above
  // it, which is the shape every checkbox list on this platform already uses.
  if (definition.valueType === "boolean") {
    return (
      <div className="flex items-start justify-between gap-4 py-4">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <Label htmlFor={id} className="text-sm font-medium">
            {title}
          </Label>
          <SettingDescription
            id={descriptionId}
            text={description}
            isDefault={isDefault}
          />
        </div>
        <Checkbox
          id={id}
          checked={value === true}
          disabled={disabled}
          aria-describedby={descriptionId}
          onCheckedChange={(checked) => onChange(checked === true)}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 py-4">
      <div className="flex flex-col gap-0.5">
        <Label htmlFor={id} className="text-sm font-medium">
          {title}
        </Label>
        <SettingDescription
          id={descriptionId}
          text={description}
          isDefault={isDefault}
        />
      </div>

      {definition.valueType === "enum" ? (
        <Select
          value={typeof value === "string" ? value : ""}
          disabled={disabled}
          onValueChange={onChange}
        >
          <SelectTrigger id={id} aria-describedby={descriptionId} className="sm:max-w-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {definition.choices.map((choice) => (
              <SelectItem key={choice} value={choice}>
                {valueLabel(choice)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : null}

      {definition.valueType === "timezone" ? (
        <TimeZoneControl
          id={id}
          describedBy={descriptionId}
          value={typeof value === "string" ? value : "UTC"}
          disabled={disabled}
          onCommit={onChange}
        />
      ) : null}

      {definition.valueType === "text" ? (
        <TextControl
          id={id}
          describedBy={descriptionId}
          value={typeof value === "string" ? value : ""}
          maxLength={definition.maxLength ?? undefined}
          disabled={disabled}
          onCommit={onChange}
        />
      ) : null}

      {definition.valueType === "string_list" ? (
        <StringListControl
          idPrefix={id}
          describedBy={descriptionId}
          value={Array.isArray(value) ? value : []}
          choices={definition.choices}
          disabled={disabled}
          onChange={onChange}
        />
      ) : null}
    </div>
  );
}

function SettingDescription({
  id,
  text,
  isDefault,
}: {
  id: string;
  text?: string;
  isDefault: boolean;
}) {
  return (
    <p id={id} className="text-sm text-muted-foreground">
      {text}
      {/* Stated rather than implied. An account that has never opened this page
          has no stored row at all, and showing a value as chosen when nobody
          chose it would suggest somebody did. */}
      {isDefault ? <span className="italic"> (platform default)</span> : null}
    </p>
  );
}

/**
 * Free text that commits on blur.
 *
 * Saving per keystroke would be one request per character. Keeping a local draft
 * and committing when focus leaves is the first moment somebody has finished
 * typing — and the draft is re-seeded from the prop, so a failed save that
 * restores the old value is reflected here rather than leaving the field showing
 * text the server rejected.
 */
function TextControl({
  id,
  describedBy,
  value,
  maxLength,
  disabled,
  onCommit,
}: {
  id: string;
  describedBy: string;
  value: string;
  maxLength?: number;
  disabled: boolean;
  onCommit: (value: string) => void;
}) {
  const [draft, setDraft] = React.useState(value);
  // Re-seeded **during render** rather than in an effect: an effect that calls
  // setState runs a second render pass every time the server's answer changes,
  // and React's own guidance is to derive from props instead. Tracking the last
  // prop seen is the documented "adjust state when a prop changes" pattern.
  const [lastValue, setLastValue] = React.useState(value);
  if (lastValue !== value) {
    setLastValue(value);
    setDraft(value);
  }

  return (
    <Textarea
      id={id}
      aria-describedby={describedBy}
      value={draft}
      maxLength={maxLength}
      disabled={disabled}
      rows={3}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => {
        if (draft !== value) onCommit(draft);
      }}
    />
  );
}

/**
 * A time zone, offered as a short list plus free entry.
 *
 * **Not a `<Select>` of every IANA zone**, deliberately: there are nearly six
 * hundred, a dropdown of them is unusable, and shipping the list would be
 * shipping a copy of a database that changes when countries change their rules.
 * The offered options come from `Intl.supportedValuesOf`, which is the browser's
 * *own* tz database — so a zone the runtime cannot render is never offered — with
 * a small hard-coded fallback for runtimes that do not implement it.
 *
 * The input is free text against a `datalist` rather than a closed picker,
 * because the API validates against Python's tz database and the two can differ:
 * refusing a value here that the server would accept would be this client
 * inventing a rule.
 */
function TimeZoneControl({
  id,
  describedBy,
  value,
  disabled,
  onCommit,
}: {
  id: string;
  describedBy: string;
  value: string;
  disabled: boolean;
  onCommit: (value: string) => void;
}) {
  const [draft, setDraft] = React.useState(value);
  // Derived during render — see `TextControl` for why this is not an effect.
  const [lastValue, setLastValue] = React.useState(value);
  if (lastValue !== value) {
    setLastValue(value);
    setDraft(value);
  }

  const zones = React.useMemo(() => {
    const supported = (
      Intl as unknown as { supportedValuesOf?: (key: string) => string[] }
    ).supportedValuesOf;
    if (typeof supported === "function") {
      try {
        return supported("timeZone");
      } catch {
        // Fall through to the short list.
      }
    }
    return ["UTC", "Africa/Casablanca", "Europe/Paris", "Europe/London", "America/New_York"];
  }, []);

  const listId = `${id}-zones`;

  return (
    <>
      <Input
        id={id}
        aria-describedby={describedBy}
        list={listId}
        value={draft}
        disabled={disabled}
        className="sm:max-w-xs"
        placeholder="Europe/Paris"
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => {
          if (draft !== value) onCommit(draft);
        }}
      />
      <datalist id={listId}>
        {zones.map((zone) => (
          <option key={zone} value={zone} />
        ))}
      </datalist>
    </>
  );
}

/**
 * A list of identifiers, rendered as a checkbox grid.
 *
 * **An empty selection is meaningful and is said out loud.** For visible widgets
 * it means "everything my role allows, including anything added later" rather
 * than "nothing" — which is the whole reason the server's default is an empty
 * list rather than all nineteen keys. A control that read blank as "hide
 * everything" would make the default unreachable once somebody had touched it.
 */
function StringListControl({
  idPrefix,
  describedBy,
  value,
  choices,
  disabled,
  onChange,
}: {
  idPrefix: string;
  describedBy: string;
  value: string[];
  choices: string[];
  disabled: boolean;
  onChange: (value: string[]) => void;
}) {
  const valueLabel = useValueLabel();
  const selected = new Set(value);

  return (
    <div
      role="group"
      aria-describedby={describedBy}
      className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3"
    >
      {choices.map((choice) => {
        const id = `${idPrefix}-${choice}`;
        return (
          <div key={choice} className="flex items-center gap-2">
            <Checkbox
              id={id}
              checked={selected.has(choice)}
              disabled={disabled}
              onCheckedChange={(checked) => {
                // Order is preserved by filtering the original list rather than
                // rebuilding from the set: a list somebody arranged is a list
                // somebody arranged.
                const next =
                  checked === true
                    ? [...value, choice]
                    : value.filter((entry) => entry !== choice);
                onChange(next);
              }}
            />
            <Label htmlFor={id} className="text-sm font-normal">
              {valueLabel(choice)}
            </Label>
          </div>
        );
      })}
    </div>
  );
}
