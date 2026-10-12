// Curated XXX/USD currency pairs supported by forex v1.
// [symbol, 中文名, English name, 一句话介绍]
const RAW = {
  '外汇/美元报价': [
    ['EURUSD=X', '欧元/美元', 'Euro / US Dollar', '报价表示 1 欧元值多少美元；买入看涨欧元，卖空看跌欧元'],
    ['GBPUSD=X', '英镑/美元', 'British Pound / US Dollar', '报价表示 1 英镑值多少美元；买入看涨英镑，卖空看跌英镑'],
    ['AUDUSD=X', '澳元/美元', 'Australian Dollar / US Dollar', '报价表示 1 澳元值多少美元；买入看涨澳元，卖空看跌澳元'],
    ['NZDUSD=X', '新西兰元/美元', 'New Zealand Dollar / US Dollar', '报价表示 1 新西兰元值多少美元；买入看涨新西兰元，卖空看跌新西兰元'],
  ],
};

export const SECTORS = Object.keys(RAW);
export const PAIRS = Object.entries(RAW).flatMap(([sector, rows]) =>
  rows.map(([symbol, cn, en, desc]) => ({ symbol, cn, en, desc, sector })));
export const BY_SYMBOL = Object.fromEntries(PAIRS.map((p) => [p.symbol, p]));
