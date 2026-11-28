#!/bin/bash
# Worker1 status check
cd ~/.openclaw/workspace/reading-list

echo "=== $(date '+%H:%M:%S') Worker #1 Status ==="
echo
echo "--- worker1_main (sweep progress) ---"
tail -5 logs/worker1_main.log 2>/dev/null
echo
echo "--- current worker1 book ---"
if ls logs/worker1_b*.log 2>/dev/null > /dev/null; then
  latest=$(ls -t logs/worker1_b*.log 2>/dev/null | head -1)
  echo "File: $latest"
  echo "Last 5 lines:"
  tail -5 "$latest"
fi
echo
echo "--- parent workers ---"
pgrep -fl "single_book_pipeline.py run" | grep -v grep | awk '{print "  PID " $1 ": " $NF}' | head -10
echo
echo "--- processed count ---"
python3 -c "import json; d=json.load(open('processed_books.json')); print('Total processed:', len(d.get('books', {})))"
echo
echo "--- my 10 books status ---"
python3 -c "
import json
d = json.load(open('processed_books.json'))
books = d.get('books', {})
my_books = ['欢乐英雄','动物庄园','次贷危机中的女人','项塔兰[全三册]','决战朝鲜','自私的基因','大医·日出篇——马伯庸','俗女养成记','宋慈洗冤笔记（独家首发｜1-3册）','人类的群星闪耀时']
for b in my_books:
    matches = [k for k in books if b.replace(' ','').replace('·','').replace('[全三册]','').replace('（独家首发｜1-3册）','').replace('——马伯庸','').replace('（上下册）','').lower() in k.replace(' ','').replace('·','').replace('[全三册]','').replace('（独家首发｜1-3册）','').replace('——马伯庸','').replace('（上下册）','').lower() or k.replace(' ','').replace('·','').replace('[全三册]','').replace('（独家首发｜1-3册）','').replace('——马伯庸','').replace('（上下册）','').lower() in b.replace(' ','').replace('·','').replace('[全三册]','').replace('（独家首发｜1-3册）','').replace('——马伯庸','').replace('（上下册）','').lower()]
    status = '✅ DONE' if matches else '⏳ pending'
    print(f'  {status} 《{b}》')
"
