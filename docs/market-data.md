# 行情支持范围与口径

基本面仍限于内地A股公司；历史行情另外支持境内外指数和商品。新增数据直接使用公开腾讯及新浪端点，不新增SDK或付费账户。通过聊天按需查询，复用本地缓存和任务来源归档。目录中共56个规范代码，沪深300两个市场代码代表同一指数。

## 内地指数

| 名称 | 代码 | 报价单位 |
|---|---|---|
| 上证指数 | `000001.SH` | 点 |
| 深证成指 | `399001.SZ` | 点 |
| 创业板指 | `399006.SZ` | 点 |
| 沪深300 | `000300.SH` | 点 |
| 沪深300 | `399300.SZ` | 点 |
| 上证50 | `000016.SH` | 点 |
| 中证500 | `000905.SH` | 点 |
| 中证1000 | `000852.SH` | 点 |
| 科创50 | `000688.SH` | 点 |
| 中小100 | `399005.SZ` | 点 |

## 境外指数

| 名称 | 代码 | 报价单位 |
|---|---|---|
| 道琼斯工业指数 | `DJI.US` | 点 |
| 标普500 | `SPX.US` | 点 |
| 纳斯达克综合指数 | `IXIC.US` | 点 |
| 恒生指数 | `HSI.HK` | 点 |
| 恒生科技指数 | `HSTECH.HK` | 点 |
| 日经225 | `N225.JP` | 点 |
| 韩国综合指数 | `KOSPI.KR` | 点 |
| 英国富时100 | `FTSE.UK` | 点 |
| 德国DAX | `DAX.DE` | 点 |
| 法国CAC40 | `CAC.FR` | 点 |
| 欧洲斯托克50 | `STOXX50.EU` | 点 |

纳斯达克100（NDX.US）暂未启用：实测腾讯响应附带了其他股票身份，系统拒绝接受；替代公开来源未验证成功。品种目录的unavailable说明原因，不能用纳斯达克综合指数替代。

## 国内商品期货

| 名称 | 代码 | 报价单位 |
|---|---|---|
| 多晶硅主力连续 | `PS0.GFEX` | 元/吨 |
| 碳酸锂主力连续 | `LC0.GFEX` | 元/吨 |
| 工业硅主力连续 | `SI0.GFEX` | 元/吨 |
| 生猪主力连续 | `LH0.DCE` | 元/吨 |
| 沪金主力连续 | `AU0.SHFE` | 元/克 |
| 沪银主力连续 | `AG0.SHFE` | 元/千克 |
| 铜主力连续 | `CU0.SHFE` | 元/吨 |
| 铝主力连续 | `AL0.SHFE` | 元/吨 |
| 锌主力连续 | `ZN0.SHFE` | 元/吨 |
| 镍主力连续 | `NI0.SHFE` | 元/吨 |
| 氧化铝主力连续 | `AO0.SHFE` | 元/吨 |
| 螺纹钢主力连续 | `RB0.SHFE` | 元/吨 |
| 天然橡胶主力连续 | `RU0.SHFE` | 元/吨 |
| 上海原油主力连续 | `SC0.INE` | 元/桶 |
| 铁矿石主力连续 | `I0.DCE` | 元/吨 |
| 焦炭主力连续 | `J0.DCE` | 元/吨 |
| 焦煤主力连续 | `JM0.DCE` | 元/吨 |
| 豆粕主力连续 | `M0.DCE` | 元/吨 |
| 玉米主力连续 | `C0.DCE` | 元/吨 |
| 棕榈油主力连续 | `P0.DCE` | 元/吨 |
| 豆油主力连续 | `Y0.DCE` | 元/吨 |
| 鸡蛋主力连续 | `JD0.DCE` | 元/500千克 |
| 棉花主力连续 | `CF0.CZCE` | 元/吨 |
| 白糖主力连续 | `SR0.CZCE` | 元/吨 |
| PTA主力连续 | `TA0.CZCE` | 元/吨 |
| 甲醇主力连续 | `MA0.CZCE` | 元/吨 |
| 纯碱主力连续 | `SA0.CZCE` | 元/吨 |
| 玻璃主力连续 | `FG0.CZCE` | 元/吨 |
| 尿素主力连续 | `UR0.CZCE` | 元/吨 |

默认查询供应商主力连续序列（代码中的0），不是一张固定可交易合约。可指定月份，如PS2611.GFEX、LC2611.GFEX、LH2611.DCE。合同是否上市及来源是否有数据以实际查询为准，不由目录推断。除郑商所可以接受三位年月外，具体合约使用四位年月；月份必须为01至12，品种须匹配交易所。

## 外盘商品与黄金现货

| 名称 | 代码 | 报价单位 |
|---|---|---|
| COMEX黄金 | `GC.COMEX` | 美元/金衡盎司 |
| COMEX白银 | `SI.COMEX` | 美元/金衡盎司 |
| WTI原油 | `CL.NYMEX` | 美元/桶 |
| 布伦特原油 | `BRN.ICE` | 美元/桶 |
| NYMEX天然气 | `NG.NYMEX` | 美元/百万英热单位 |
| 伦敦金现货 | `XAU.SPOT` | 美元/金衡盎司 |

外盘期货当前使用供应商连续报价序列（provider_continuous），未适配具体交割月份合约；伦敦金为现货口径（spot）。纽约金和伦敦金分别处理，生猪期货不是猪肉现货价格。黄金、原油、SI等歧义名称必须先查目录。

## 查询方式

- search_market_symbols：按query、security_type、limit、offset分页检索已接入品种，目录不是报价，不产生行情来源。
- get_market_bars：读取日/周/月历史行情，默认250根，最多返回640根。股票默认qfq；指数、期货和现货仅none。
- get_market_technicals：同一缓存计算MA5/10/20/60、MACD(12,26,9)、Wilder RSI14和可用量价信号。
- get_stock_fundamentals：保留A股公司概况、主营和财务查询；指数和商品不能使用公司财务工具。

旧get_stock_bars/get_stock_technicals仍可执行旧检查点调用，新工具定义使用通用市场名称。HTTP支持GET /api/market/instruments及POST /api/market/bars、/api/market/technicals；旧/api/stocks/bars和/api/stocks/technicals仍有效。

```json
{"symbol":"LH0.DCE","period":"day","limit":5}
```

## 日期、价格与成交量

国内股票和指数保留腾讯原生周期。新增境外指数与商品首先读取日线，周线按来源日期的ISO周、月线按自然月聚合。开盘取首值、收盘取末值、高低取极值；成交量在完整可用时求和，持仓和结算价取末值。历史查询先截断结束日期再聚合，不引入截止日之后的数据；开始日期按周期末交易日期筛选，首尾周期可能不完整。

腾讯境外指数日线最多640根，新浪全球指数约1000根；国内外期货端点返回来源可用历史，受4MB响应限制。返回coverage.source_daily_rows、upstream_daily_limit、source_limited、source_start_date/source_end_date及实际返回范围；来源有截断时不能声称取得全部历史。

时间分别记录来源交易日期、市场time_zone和抓取fetched_at。美股最新可用交易日可能是北京时间前一天；最新日/周/月可能未结束。国内夜盘在晚间允许来源标注随后三个自然日内的下一交易日，并提示未收盘；不是预测未来价格。

指数点位不是人民币股价。国内商品按品种单位报价，成交量及持仓按来源手数返回，不乘股票的100。close与settlement不能混用：前者为来源收盘/最新日线价，后者为来源结算价；没有前结算价时不能称为交易所口径涨跌幅。周期聚合的结算价是最后一日值，不是周期加权结算价。

境外指数数量字段可能是成交额，不能自动认定为成交量；外盘商品的零成交量常为占位值。此类数据保留source_quantity，未核实或缺失volume为null，缺失结算与持仓也为null。技术指标遇到缺失成交量不计算量比，不填零。

## 数据质量与缓存

固定端点、规范供应商代码和腾讯附带身份共同约束品种；跨市场身份不匹配明确失败。只解析JSONP固定包装中的JSON，不执行脚本。非有限价格、重复日期、异常未来日期、负数量和不支持的格式失败并记录诊断，取消向上传递。期货历史可以出现负价格。

来源OHLC不一致时保留原值、记录warning日志，返回行quality_flags及quality.flagged_dates/flagged_periods；不擅自调整高低价或删除异常日期。价格指标仅按来源收盘值计算，相关20周期中有异常时停用突破判断。连续期货主力映射和换月调整尚未核实，停用突破信号，MACD等价格指标也可能受换月跳变影响。

行情继续使用research.sqlite中的stock_data_cache，5分钟有效，可refresh强制更新；行情缓存版本升级以避免复用旧元数据，财务缓存不变。连续、具体合约和不同市场分别缓存。来源故障时最多回退1天，保留原抓取时间与质量警告并注明stale；无缓存则明确失败。原始响应和本次结果按任务归档JSON，结构化来源不替代公司研究的公告原文。

## 协议与单位依据

- [新浪国内期货协议](https://github.com/akfamily/akshare/blob/main/akshare/futures/futures_zh_sina.py)、[外盘日线协议](https://github.com/akfamily/akshare/blob/main/akshare/futures/futures_foreign.py)、[全球指数协议](https://github.com/akfamily/akshare/blob/main/akshare/index/index_global_sina.py)。实现独立读取公开端点，不依赖这些模块。
- [碳酸锂合约](https://www.gfex.com.cn/gfex/sytslqhhy/202307/9ad927b8ec4c458594e172fd1ada2a9b.shtml)、[生猪期货业务细则](https://www.dce.com.cn/dalianshangpin/fgfz/6142914/6142926/6262877/%E5%A4%A7%E8%BF%9E%E5%95%86%E5%93%81%E4%BA%A4%E6%98%93%E6%89%80%E7%94%9F%E7%8C%AA%E6%9C%9F%E8%B4%A7%E4%B8%9A%E5%8A%A1%E7%BB%86%E5%88%99%EF%BC%88%E6%A0%B9%E6%8D%AE2025%E5%B9%B46%E6%9C%8824%E6%97%A5%E3%80%942025%E3%80%9559%E5%8F%B7%E6%96%87%E4%BB%B6%E4%BF%AE%E6%94%B9%EF%BC%89.pdf)。
- [沪金单位](https://www.shfe.com.cn/products/futures/metal/ferrousandpreciousmetal/)、[沪银单位](https://www.shfe.cn/regulation/exchangerules/productrules/202512/t20251231_829961.html)、[上海原油合约](https://www.ine.com.cn/products/futures/energyandchemical/sc_f/standard_sc_f/202312/t20231205_802540.html)。
- [COMEX黄金](https://www.cmegroup.com/markets/metals/precious/gold-futures.html)、[布伦特原油](https://www.ice.com/products/219)、[美国天然气](https://www.cmegroup.com/markets/energy/natural-gas/natural-gas.timeAndSales.html)。

2026-10-14：46个新增规范代码完成日/周/月共138次真实响应样本解析，在线验证了用户关注的连续与月份合约、黄金原油及代表性境外指数。这验证接口、身份、格式和程序处理，不代表已经对照交易所逐条核验所有历史价格。当前仍为按需研究查询，没有实时逐笔、自动监控调度或交易执行。
