import time as t
lastTime=t.time()
while True:
    input()
    print(60/(t.time()-lastTime))
    lastTime=t.time()