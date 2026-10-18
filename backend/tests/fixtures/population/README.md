# Population fixtures

`wide.csv` is entirely synthetic and demonstrates semicolon parsing, leading-zero LOR codes, genuine zeroes, suppression and nonoverlapping cohorts. Its values are not Berlin research findings.

Contract tests generate small synthetic XLSX workbooks using the reviewed official report layout. No full provider workbook is committed.

The reviewed live configuration points to the actual official workbook linked by <https://www.statistik-berlin-brandenburg.de/a-i-16-hj/> on 18 October 2026: `SB_A01-16-00_2026h01_BE.xlsx`, worksheet `T2`. Its `Impressum` states CC BY 3.0 DE and defines `–` as no quantity, `•` as unknown or confidential, `/` as insufficiently reliable, `…` as available later, and `x` as not applicable. The connector retains these source markers in quality flags. Other annotations fail rather than being guessed.

The table has 542 Planungsräume. Four two-digit components form each eight-digit LOR identifier. One source component is numeric (`T2!D128 = 31`); the reviewed mapping explicitly permits numeric components with width two. The source includes numeric zeroes for Pankower Tor. District headings and totals have no district component and are excluded. The nine outputs per area are one population total plus eight disjoint age bands; totals must not be summed with components.
