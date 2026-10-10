# StockSim

鏈湴缇庤偂妯℃嫙浜ゆ槗灏忔父鎴忋€傜湡瀹炶鎯咃紙yfinance锛夛紝铏氭嫙璧勯噾銆?
## 鐜╂硶

鍙屽嚮 `start.bat`锛氶娆¤繍琛屼細鑷姩鍒涘缓 Python 鐜骞跺畨瑁呬緷璧栵紝鐒跺悗鎵撳紑娴忚鍣紙http://127.0.0.1:8765/锛夈€傚叧鎺夐粦鑹茬獥鍙ｅ嵆閫€鍑恒€?
- **瀹炴椂璐︽埛**锛氱編涓?09:30鈥?6:00 浜ゆ槗鏃舵鍐呯敤鏈€鏂拌鎯呬氦鏄撱€傚競浠峰崟绔嬪嵆鎴愪氦锛涢檺浠峰崟鍦ㄧ▼搴忚繍琛屾湡闂存瘡 30 绉掓鏌ヤ竴娆★紝褰撴棩鏈夋晥銆?- **鍘嗗彶鍥炴斁**锛氱偣"鏂板洖鏀?锛屼粠杩囧幓鏌愬ぉ寮€灞€锛堝彲闅忔満锛夛紝鐢?鍗婂ぉ / 1 澶?/ 1 鍛?/ 1 鏈?鎺ㄨ繘鏃堕棿锛屾渶鍚庡拰鍚屾湡鎸佹湁 SPY 姣旀敹鐩娿€傛敹甯傛椂涔熻兘鐜┿€?
瀛樻。鍦?`data/stocksim.db`锛屽垹闄ゅ嵆鍏ㄩ儴閲嶆潵銆?
## 寮€鍙?
```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python scripts\smoke.py   # 鐪熷疄琛屾儏绔埌绔鏌ワ紙浣跨敤涓存椂鏁版嵁搴擄級
```

璁捐涓庤鍒欒 [DESIGN.md](DESIGN.md)銆傝鎯呮潵鑷?Yahoo Finance 闈炲畼鏂规帴鍙ｏ紝浠呬緵涓汉浣跨敤銆?