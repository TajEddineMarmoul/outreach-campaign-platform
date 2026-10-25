import { TEMPLATE_VARIABLE_PATTERN, extractTemplateVariables, type TemplateValidationSummary } from "./templateVariables";

export interface EmailTemplate {
  id: number;
  title: string;
  subject: string;
  body: string;
  updated_at?: string;
}

export interface MappingField { name: string; missing: number; total: number; }

export function mappingFields(summary?: TemplateValidationSummary): MappingField[] {
  if (!summary) return [];
  const missing = new Map([...summary.used_warnings, ...summary.other_warnings].map((warning) => [warning.column, warning.empty_count]));
  return [...new Set(summary.all_columns)].map((name) => ({ name, missing: missing.get(name) || 0, total: summary.total_contacts }))
    .sort((a, b) => Number(a.missing > 0) - Number(b.missing > 0) || a.name.localeCompare(b.name));
}

function normalized(name: string): string {
  return name.normalize("NFKC").toLocaleLowerCase("en").replace(/[^\p{L}\p{N}]/gu, "");
}

function abbreviated(name: string): string | null {
  const tokens = name.normalize("NFKC").replace(/([a-z])([A-Z])/g, "$1 $2").toLocaleLowerCase("en").split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  return tokens.length > 1 ? tokens.slice(0, -1).map((part) => part[0]).join("") + tokens.at(-1) : null;
}

/** Suggestions exist only at template import. Delivery keeps exact header matching. */
export function suggestVariableMapping(variables: string[], fields: MappingField[]): Record<string, string> {
  return Object.fromEntries(variables.map((source) => {
    if (fields.some((field) => field.name === source)) return [source, source];
    const equivalent = fields.filter((field) => normalized(field.name) === normalized(source));
    if (equivalent.length === 1) return [source, equivalent[0].name];
    if (equivalent.length > 1) return [source, ""];
    const abbreviatedMatches = fields.filter((field) => normalized(field.name) === abbreviated(source) || normalized(source) === abbreviated(field.name));
    return [source, abbreviatedMatches.length === 1 ? abbreviatedMatches[0].name : ""];
  }));
}

export function templateVariables(template: Pick<EmailTemplate, "subject" | "body">): string[] {
  return [...new Set([...extractTemplateVariables(template.subject), ...extractTemplateVariables(template.body)])];
}

export function remapTemplateText(value: string, mapping: Record<string, string>): string {
  // One pass prevents A→B and B→C from accidentally changing A twice.
  return value.replace(TEMPLATE_VARIABLE_PATTERN, (match, variable: string) => {
    const name = variable.trim();
    return Object.hasOwn(mapping, name) && mapping[name] ? `{{ ${mapping[name]} }}` : match;
  });
}
