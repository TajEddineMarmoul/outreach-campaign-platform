export const TEMPLATE_VARIABLE_PATTERN = /\{\{\s*([^{}]+?)\s*\}\}/g;

export function templateVariableName(variable: string): string {
  return variable.trim();
}

export function extractTemplateVariables(template: string): string[] {
  return [...template.matchAll(TEMPLATE_VARIABLE_PATTERN)].map((match) =>
    templateVariableName(match[1])
  );
}

export interface TemplateValidationSummary {
  total_contacts: number;
  all_columns: string[];
  used_warnings: { column: string; empty_count: number }[];
  other_warnings: { column: string; empty_count: number }[];
}

export function templateValueWarnings(
  templates: string[],
  summary?: TemplateValidationSummary,
): { column: string; empty_count: number }[] {
  if (!summary?.total_contacts) return [];
  const emptyCounts = new Map(
    [...summary.used_warnings, ...summary.other_warnings].map((warning) => [
      warning.column, warning.empty_count,
    ]),
  );
  const columns = new Set(summary.all_columns);
  return [...new Set(templates.flatMap(extractTemplateVariables))]
    .map((column) => ({
      column,
      empty_count: columns.has(column)
        ? emptyCounts.get(column) || 0
        : summary.total_contacts,
    }))
    .filter((warning) => warning.empty_count > 0);
}

export function missingTemplateVariables(
  template: string,
  fields: Record<string, unknown>,
): string[] {
  return [...new Set(extractTemplateVariables(template))].filter((name) => {
    const value = Object.hasOwn(fields, name) ? fields[name] : undefined;
    return value === undefined || value === null || String(value).trim() === "";
  });
}
