"use client";

import { useEffect, useMemo, useState } from "react";
import { CatalogItem, client } from "@/lib/api";

type Props = {
  kind: "motors" | "propellers" | "battery-cells" | "escs";
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
  placeholder?: string;
};

export function SearchableSelect({
  kind,
  value,
  onChange,
  disabled,
  placeholder,
}: Props) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<CatalogItem[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (disabled) return;
    let cancelled = false;
    const t = setTimeout(async () => {
      setLoading(true);
      try {
        const res = await client.catalog(kind, q);
        if (!cancelled) setItems(res.items);
      } catch {
        if (!cancelled) setItems([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 180);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [kind, q, disabled]);

  const selectedLabel = useMemo(() => {
    const hit = items.find((i) => i.id === value);
    return hit?.label || value || placeholder || "Select…";
  }, [items, value, placeholder]);

  if (disabled) {
    return (
      <div className="field opacity-50" aria-disabled>
        Optimize automatically
      </div>
    );
  }

  return (
    <div className="relative">
      <button
        type="button"
        className="field text-left"
        onClick={() => setOpen((v) => !v)}
      >
        {selectedLabel}
      </button>
      {open && (
        <div className="panel absolute z-20 mt-1 max-h-64 w-full overflow-auto p-2 shadow-xl">
          <input
            className="field mb-2"
            placeholder="Search manufacturer / model / id"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            autoFocus
          />
          {loading && <div className="px-2 py-1 text-sm text-[var(--muted)]">Loading…</div>}
          {!loading && items.length === 0 && (
            <div className="px-2 py-1 text-sm text-[var(--muted)]">No evaluable parts</div>
          )}
          {items.map((item) => (
            <button
              key={item.id}
              type="button"
              className="block w-full rounded px-2 py-2 text-left text-sm hover:bg-white/5"
              onClick={() => {
                onChange(item.id);
                setOpen(false);
              }}
            >
              <div>{item.label}</div>
              <div className="text-xs text-[var(--muted)]">{item.id}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
