# fix_csv.py
import csv, sys

def fix(src, dst):
    with open(src, 'r', newline='', encoding='utf-8', errors='replace') as fin, \
         open(dst, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.reader(fin, escapechar='\\')
        writer = csv.writer(fout, quoting=csv.QUOTE_MINIMAL)
        for i, row in enumerate(reader):
            writer.writerow(row)
            if i % 500000 == 0:
                print(f"{i} rows...", flush=True)
    print("done")

fix('/tmp/movie_info.csv', '/tmp/movie_info_fixed.csv')
fix('/tmp/person_info.csv', '/tmp/person_info_fixed.csv')
