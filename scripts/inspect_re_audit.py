import re
import sys
import os

log_file = r"C:\Users\nithi\.gemini\antigravity\brain\54ea6d64-2aa6-4ae5-9ae9-2904735ca905\.system_generated\tasks\task-1943.log"

with open(log_file, "r", encoding="utf-8") as f:
    text = f.read()

pattern = re.compile(
    r"\[(\d+)\]\s+\[(.*?)\]\s+Card:\s+(.*?)\n"
    r"\s+Vendor A \((.*?), Rs\. (.*?)\):\s+(.*?)\n"
    r"\s+Vendor B \((.*?), Rs\. (.*?)\):\s+(.*?)\n"
    r"\s+Verdict:\s+Match=(.*?)\s+\|\s+Score=(.*?)\s+\|\s+Reason:\s+(.*?)\n"
)

matches = pattern.findall(text)
print(f"Total parsed merges: {len(matches)}")

for m in matches:
    idx, query, card, va, pa, ta, vb, pb, tb, verdict, score, reason = m
    print(f"[{int(idx):03d}] [{query}] ({va}: Rs {pa}) vs ({vb}: Rs {pb})")
    print(f"     A: {ta}")
    print(f"     B: {tb}")
    print(f"     Reason: {reason}\n")
