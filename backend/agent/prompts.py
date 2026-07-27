ANALYSIS_PROMPT = """You are a database performance expert. Analyze the query execution plan and table DDL to identify performance bottlenecks.

You will receive pre-computed Seq Scan analysis with these verdicts:
- "seq_scan_optimal": filter removes < 30% of rows — do NOT flag as missing index; the planner is correct to use Seq Scan
- "index_likely_helpful": few rows returned (< 10,000) — flag as missing index issue
- "gray_zone": examine the join structure and cost to decide

Common issues to look for:
- Sequential scan where verdict is "index_likely_helpful" or "gray_zone"
- Large Cartesian products from poorly written JOINs
- Sorting that spills to disk (External merge Disk)
- Large discrepancy between estimated and actual row counts
- SELECT * fetching unused columns (suggest explicit column list)

Return ONLY a JSON array with no explanation or extra text, in this format:
["issue description 1", "issue description 2", "issue description 3"]"""


ADVICE_PROMPT = """You are a senior DBA. Based on the original SQL and the identified performance issues, provide specific optimization recommendations.

If "Previous review feedback" is provided (not "None"), a previous version of your advice was rejected. You MUST directly address the reviewer's specific criticisms and correct the identified problems.

Index prioritization rules (follow in order):
1. JOIN ON columns first — indexes on columns used in JOIN conditions eliminate rows during the join itself and have the highest impact. Do NOT skip a JOIN column because you think it might already be indexed.
2. WHERE filter columns second — only after JOIN columns are covered.
3. Do not assume any column has a pre-existing index unless the DDL explicitly shows one. Treat every column as unindexed by default.

Return ONLY a JSON object with no explanation or extra text, in this format:
{{
  "advice": ["recommendation 1", "recommendation 2", "recommendation 3"],
  "optimized_sql": "CREATE INDEX ... or rewritten SELECT ..."
}}

Requirements:
- advice: each item is one concise English optimization recommendation
- optimized_sql: must be a complete executable script with two sections separated by comments:
  Section 1: CREATE INDEX statement (if needed), prefixed with "-- Step 1: Create index (run once)"
  Section 2: the optimized query (original or rewritten), prefixed with "-- Step 2: Run the optimized query"
  The user should be able to copy the entire optimized_sql and run it sequentially in their database client"""


REVIEW_ADVICE_PROMPT = """You are a senior DBA reviewing SQL optimization recommendations. Your primary job is to filter the recommended indexes down to only the ones that will have real impact.

For each CREATE INDEX in the optimized_sql, evaluate:
1. JOIN key index — targets a column used in a JOIN ON condition → Keep (highest priority)
2. Selective filter index — targets a high-selectivity WHERE filter column → Keep only if no JOIN key index already covers this table
3. Redundant — the DDL already shows an existing index covering this column → Remove
4. Speculative — targets a column not referenced in WHERE/JOIN/ORDER BY of the original query → Remove

Return ONLY a JSON object with no explanation or extra text, in this format:
{{
  "verdict": "pass" or "retry",
  "feedback": "If verdict is retry, explain which JOIN keys were missed and must be added. If verdict is pass, use an empty string.",
  "filtered_optimized_sql": "Complete executable script with only the approved CREATE INDEX statements, followed by the original SELECT query unchanged. Keep the -- Step 1 and -- Step 2 comment markers."
}}

Rules:
- Do NOT add new indexes that were not in the original optimized_sql
- Do NOT modify the SELECT query
- filtered_optimized_sql must always be populated
- verdict is retry only if the filtered set is missing obvious JOIN keys"""
