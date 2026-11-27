const dollars = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

export function cents(value: number | null | undefined): string {
  return value == null ? "-" : dollars.format(value / 100);
}

/** Millionths of a dollar, which is how model costs are stored. */
export function micros(value: number | null | undefined): string {
  if (value == null) {
    return "-";
  }
  const usd = value / 1_000_000;
  return usd >= 1 ? dollars.format(usd) : `$${usd.toFixed(4)}`;
}

export function when(iso: string | null | undefined): string {
  if (!iso) {
    return "-";
  }
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function percent(value: number | null | undefined): string {
  if (value == null) {
    return "-";
  }
  const digits = value > 0 && value < 0.1 ? 1 : 0;
  return `${(value * 100).toFixed(digits)}%`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "-";
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export function humanize(value: string): string {
  return value.replaceAll("_", " ");
}
