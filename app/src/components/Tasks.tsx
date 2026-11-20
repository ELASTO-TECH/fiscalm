"use client";
import { surfaceStyle } from "./Controls";
import { useCallback } from "react";
import { getTasks } from "@/lib/api";
import { parts } from "@/lib/types";
import { useApp } from "./context";
import { taskCardNode } from "./Home";
import { PageNav, usePage } from "./Paging";
import { Empty, Title } from "./ui";

export function Tasks() {
  const { view } = useApp();
  const read = useCallback((cursor: string) => getTasks(view.co, cursor), [view]);
  const page = usePage(read, 0, view.co);
  const ss = parts(view);
  return <div className="narrow max-w-none">
    <Title>Sarcini {page.total ? <small>{page.total}</small> : null}</Title>
    <PageNav page={page} />
    <div className="asks tasks">{page.data.map((entry) => {
      const source = ss.find((s) => s.co === entry.company);
      return source ? taskCardNode(source, entry, { tag: ss.length > 1 }) : null;
    })}</div>
    {!page.loading && !page.error && !page.total ? <div className={surfaceStyle}><Empty ok /></div> : null}
  </div>;
}
