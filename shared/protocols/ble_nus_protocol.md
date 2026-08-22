# BLE NUS 二进制帧协议规范

> 本文档定义铁路巡检智能眼镜系统中「基础眼镜 (ESP32-S3)」与「挎包终端 (CM4)」之间基于 BLE NUS
> (Nordic UART Service) 的二进制帧传输协议。C 固件与 Python 终端共享同一帧格式，确保跨平台一致性。

## 1. 概述

| 项目 | 说明 |
|------|------|
| 传输介质 | BLE 4.2+ GATT，NUS Service (UUID `6E400001-B5A3-F393-E0A9-E50E24DCCA9E`) |
| 角色 | 眼镜 = 从设备 (Peripheral)；挎包终端 = 主设备 (Central) |
| MTU | 协商后默认 247 字节，ATT payload 最大 244 字节 |
| 帧方向 | 双向 (眼镜 ↔ 挎包终端) |
| 可靠性 | 语义层 ACK + 超时重传 + CRC16 校验 |

## 2. 帧格式

所有多字节字段中，**SEQ 和 LEN 使用大端序 (Big-Endian)**，**CRC16 使用小端序 (Little-Endian)**。

```
偏移   字段         长度   字节序        说明
0      SYNC         2B     -            同步头固定 0xAA 0x55
2      MSG_TYPE     1B     -            消息类型，见 message_types.h
3      SEQ          2B     Big-Endian   序列号，0x0000-0xFFFF 循环递增
5      LEN          2B     Big-Endian   DATA 段字节数 (0-65535)
7      DATA         N B    -            载荷，N = LEN
7+N    CRC16        2B     Little-Endian 对 SYNC..DATA 的 CRC-16/CCITT-FALSE 校验
```

**帧总长度** = 2 (SYNC) + 1 (MSG_TYPE) + 2 (SEQ) + 2 (LEN) + LEN (DATA) + 2 (CRC16) = **9 + LEN** 字节。

### 2.1 同步头 (SYNC)

固定两字节 `0xAA 0x55`，用于接收端在字节流中定位帧起始。接收方持续扫描，遇到 `0xAA 0x55` 即尝试解析帧。

### 2.2 消息类型 (MSG_TYPE)

单字节枚举，定义于 [`message_types.h`](message_types.h) / [`message_types.py`](message_types.py)。
共 8 个范围段 35 个值，详见对应文件。C 与 Python 枚举值严格一一对应。

### 2.3 序列号 (SEQ)

- 16 位无符号整数，大端序。
- 发送方维护独立计数器，每发送一帧自增 1，溢出后回绕到 0。
- SEQ=0x0000 保留为握手帧，握手成功后从 0x0001 开始递增。
- 接收方使用 SEQ 进行 ACK 关联和乱序检测。

### 2.4 长度 (LEN)

- 16 位无符号整数，大端序。
- 表示 DATA 段的字节数，范围 0-65535。
- 注意：受 BLE MTU 限制，单帧 DATA 通常 ≤ 240 字节；超出部分使用分包机制 (见第 4 节)。

### 2.5 CRC16 校验

- 算法：**CRC-16/CCITT-FALSE** (多项式 0x1021，初始值 0xFFFF，输入/输出不反转)。
- 计算范围：SYNC (2B) + MSG_TYPE (1B) + SEQ (2B) + LEN (2B) + DATA (N B)，即偏移 0 到 6+N。
- 存储：2 字节小端序 (低字节在前)。

```
CRC16 计算示例 (伪代码):
    crc = 0xFFFF
    for byte in frame[0 : 7+LEN]:
        crc ^= (byte << 8)
        for i in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = (crc << 1)
            crc &= 0xFFFF
    # 写入时低字节在前: frame[7+LEN] = crc & 0xFF; frame[8+LEN] = (crc >> 8) & 0xFF
```

## 3. 可靠性机制

### 3.1 ACK 确认

- 需要 ACK 的帧发送后，发送方启动 2 秒超时计时器。
- 收到对应 SEQ 的 `MSG_ACK` 帧后取消超时。
- 超时未收到 ACK 则重传，最多重试 3 次；3 次失败后触发断线重连流程。
- `MSG_ACK` 帧的 DATA 段为 2 字节被确认的 SEQ (大端序)。

### 3.2 心跳保活

- 连接建立后双方每 **10 秒** 互发一次 `MSG_HEARTBEAT`。
- DATA 段为 4 字节 Unix 时间戳 (大端序)，用于时钟粗同步。
- 连续 3 次心跳未收到响应 (30 秒) 判定连接超时，触发重连。

### 3.3 断开流程

- 主动断开方发送 `MSG_DISCONNECT`，DATA 段 1 字节原因码：
  - `0x01` 用户主动断开
  - `0x02` 低电量
  - `0x03` 错误/异常
  - `0x04` OTA 重启
- 接收方回复 `MSG_ACK` 后关闭 GATT 连接。

## 4. 分包重组

当 DATA 长度超过单帧承载能力 (MTU - 9 字节帧头开销) 时，使用分包传输。

### 4.1 分包帧结构

分包帧的 DATA 段携带分包头部：

```
偏移   字段          长度   说明
0      PACK_FLAGS    1B     bit0: 是否为分包帧 (1=是)
                            bit1: 0=首包, 1=中间包
                            bit2: 是否为末包
                            bit7: 保留
1      PACK_ID       1B     分包组 ID，同一次逻辑消息递增
2      PACK_OFFSET   2B BE  当前包数据在原始消息中的字节偏移
4      PACK_TOTAL    2B BE  原始消息总字节数
6      FRAG_DATA     M B    本片段数据 (M = LEN - 6)
```

### 4.2 重组规则

1. 接收方收到首包 (bit1=0) 时分配 `PACK_TOTAL` 大小的缓冲区。
2. 按 `PACK_OFFSET` 将 `FRAG_DATA` 写入缓冲区对应位置。
3. 收到末包 (bit2=1) 后校验总接收字节数 == `PACK_TOTAL`。
4. 重组完成后将完整 DATA 交给上层消息处理器。
5. 同一 `PACK_ID` 的分包超时 30 秒未完成则丢弃并释放缓冲区。

## 5. 连接生命周期

```
[中央设备]                                    [外设(眼镜)]
     │                                              │
     │  --- GAP 扫描发现 NUS Service 广播 --->       │
     │                                              │
     │  --- GATT Connect ---------------------->    │
     │                                              │
     │  --- Discover NUS RX/TX Characteristics -->  │
     │                                              │
     │  --- MTU Exchange (247) ------------------>  │
     │                                              │
     │  <--- MSG_HANDSHAKE_REQ (0x00) ------------- │  (外设主动握手)
     │                                              │
     │  --- MSG_HANDSHAKE_ACK (0x01) ----------->   │
     │       DATA: {version, capabilities}          │
     │                                              │
     │  === 连接就绪，开始数据传输 ===               │
     │                                              │
     │  <--- MSG_HEARTBEAT (0x02) 每10s -----------  │
     │  --- MSG_HEARTBEAT (0x02) 每10s ----------->  │
     │                                              │
     │  --- MSG_DEVICE_CONFIG (0x11) ----------->   │
     │  <--- MSG_ACK (0x0F) ----------------------  │
     │                                              │
     │  <--- MSG_CAMERA_FRAME (0x20) ------------- │  (分包)
     │  --- MSG_ACK (0x0F) ----------------------> │
     │                                              │
     │  --- MSG_DISCONNECT (0x03) ---------------> │
     │  <--- MSG_ACK (0x0F) ----------------------  │
     │                                              │
     │  --- GATT Disconnect --------------------->  │
```

## 6. 错误处理

| 错误码 | 含义 | 处理方式 |
|--------|------|---------|
| CRC 校验失败 | 传输误码 | 丢弃当前帧，不回 ACK，等待下一帧同步头 |
| MSG_TYPE 未知 | 版本不兼容 | 回复 `MSG_ACK` + 错误码，记录日志 |
| LEN 超限 | 协议异常 | 丢弃帧，重置接收状态机 |
| SEQ 乱序 | 网络抖动 | 接受并处理，记录乱序计数 |

## 7. 参考实现

- C 实现 (眼镜固件): `glasses-firmware/main/ble/ble_transport.c`
- Python 实现 (挎包终端): `bag-terminal/app/ble/message_protocol.py`
- 一致性测试: `test_consistency.py`
