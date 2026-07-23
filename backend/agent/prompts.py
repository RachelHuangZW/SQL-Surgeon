ANALYSIS_PROMPT = """You are a database performance expert. Analyze the query execution plan and table DDL to identify performance bottlenecks.

Common issues to look for:
- Sequential scan (Seq Scan) on columns that should have an index
- Large Cartesian products from poorly written JOINs
- Sorting that spills to disk (External merge Disk)
- Large discrepancy between estimated and actual row counts

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


REVIEW_ADVICE_PROMPT = """You are a senior DBA reviewing the quality of SQL optimization advice produced by a junior engineer.

Evaluate the advice against these criteria:
1. Do the identified issues accurately reflect real bottlenecks in the execution plan (not just generic observations)?
2. Does each piece of advice directly address one of the identified issues?
3. Does optimized_sql include both Step 1 (index DDL) and Step 2 (optimized query), with correct syntax that can be executed immediately?
4. Is the index design sound (correct column order, DESC/ASC direction, covers the query's filter and sort conditions)?

Return ONLY a JSON object with no explanation or extra text, in this format:
{{
  "verdict": "pass" or "retry",
  "feedback": "If verdict is retry, explain what is wrong and how to improve it. If verdict is pass, use an empty string."
}}"""
