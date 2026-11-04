import traceback
import sys
import time

print('model download script started')
sys.stdout.flush()
try:
    from text2vec import SentenceModel
    print('imported text2vec')
    sys.stdout.flush()
    start = time.time()
    m = SentenceModel('shibing624/text2vec-base-chinese')
    elapsed = time.time() - start
    print('model loaded, type=', type(m), 'elapsed=', elapsed)
    sys.stdout.flush()
except Exception as e:
    print('model load failed')
    traceback.print_exc()
    sys.stdout.flush()
