"use client";

import { useId, useRef, useState } from "react";
import { ChevronDown, Check } from "lucide-react";
import type { MappingField } from "@/lib/templateMapping";

export default function VariableMappingSelect({ source, value, fields, onChange }: {
  source: string; value: string; fields: MappingField[]; onChange: (value: string) => void;
}) {
  const id = useId();
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const options = fields.filter((field) => field.name.toLowerCase().includes(query.toLowerCase()));
  const selected = fields.find((field) => field.name === value);
  const choose = (name: string) => { onChange(name); setQuery(""); setOpen(false); };
  return <div className="template-field-select" ref={root} onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
  }}>
    <div className={`template-field-input ${selected?.missing ? "has-missing" : ""}`}>
      <input role="combobox" aria-label={`Map ${source} to audience field`} aria-expanded={open} aria-autocomplete="list"
        aria-controls={`${id}-options`} aria-activedescendant={open && options[active] ? `${id}-option-${active}` : undefined}
        value={open ? query : value} placeholder="Search or choose a field" autoComplete="off"
        onFocus={() => { setOpen(true); setQuery(""); setActive(0); }}
        onChange={(event) => { setQuery(event.target.value); setActive(0); setOpen(true); }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault(); setOpen(true);
            setActive(open ? Math.max(0, Math.min(options.length - 1, active + (event.key === "ArrowDown" ? 1 : -1))) : 0);
          } else if (event.key === "Enter") {
            event.preventDefault(); if (open && options[active]) choose(options[active].name); else setOpen(true);
          } else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); }
        }} />
      <button type="button" tabIndex={-1} aria-label={`Show fields for ${source}`} onClick={() => { root.current?.querySelector("input")?.focus(); setOpen(!open); setQuery(""); }}><ChevronDown size={16} /></button>
    </div>
    {selected && <small className={selected.missing ? "template-field-missing-text" : "app-muted"}>
      {selected.missing ? `Missing for ${selected.missing} of ${selected.total} recipients` : `Complete for all ${selected.total} recipients`}
    </small>}
    {open && <div className="template-field-options" role="listbox" id={`${id}-options`} aria-label={`Audience fields for ${source}`}>
      {options.length ? options.map((field, index) => <div key={field.name}>
        {(index === 0 || Boolean(options[index - 1].missing) !== Boolean(field.missing)) && <div className="template-field-group-label">{field.missing ? "Fields with missing values" : "Complete fields"}</div>}
        <div role="option" id={`${id}-option-${index}`} aria-selected={value === field.name}
          className={`template-field-option ${field.missing ? "has-missing" : ""} ${active === index ? "is-active" : ""}`}
          onMouseDown={(event) => event.preventDefault()} onMouseEnter={() => setActive(index)} onClick={() => choose(field.name)}>
          <span><strong>{field.name}</strong><small>{field.missing ? `${field.missing} missing values` : "No missing values"}</small></span>
          {value === field.name && <Check size={16} aria-hidden="true" />}
        </div>
      </div>) : <p className="template-field-no-results">No matching fields. Try another search.</p>}
    </div>}
  </div>;
}
