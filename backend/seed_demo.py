# -*- coding: utf-8 -*-
"""初始化演示数据：建表 + 建默认租户 + 三个演示账号 + 300 条虚构问答。

用法：
    python seed_demo.py            # 使用 DATABASE_URL 环境变量或默认本地库
    python seed_demo.py --reset    # 先清空本租户已有问答再灌

数据全部虚构：型号、参数、故障码均为示例，不对应任何真实厂商或现场。
"""
import os
import sys
import random
import argparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 演示用固定口令（生产部署请通过环境变量覆盖）
os.environ.setdefault('DEFAULT_ADMIN_PASSWORD', 'demo-admin-2026')
os.environ.setdefault('DEFAULT_EMPLOYEE_PASSWORD', 'demo-employee-2026')
os.environ.setdefault('DEFAULT_CUSTOMER_PASSWORD', 'demo-customer-2026')

import db  # noqa: E402

MODELS = ['DEMO-100', 'DEMO-200', 'DEMO-300', 'DEMO-500', 'DEMO-700']
CATEGORIES = ['参数设置', '安装接线', '报警处理', '维护保养', '选型与规格', '通讯配置']
SCENES = ['高温车间', '长电缆场合', '频繁启停', '夜间无人值守', '新产线调试']

TOPICS = {
    '参数设置': [
        ('电子齿轮比怎么算？', '电子齿轮比 = 编码器分辨率 / 电机一圈所需脉冲数 × 机械减速比。先确认编码器位数，再按上位机每圈指令脉冲反算分子分母，尽量化简为整数比，避免累计误差。'),
        ('加减速时间设多少合适？', '按负载惯量比选：惯量比小于 5 倍时加速时间可设 0.2–0.5 秒；大于 10 倍建议放宽到 1 秒以上，否则容易触发过流。先用默认值试运行，再看电流波形微调。'),
        ('转矩提升怎么设？', '低频段启动力矩不足时逐步提高转矩提升百分比，每次加 1%，观察空载电流不超过额定值的 50%。设太高会导致电机发热和低速抖动。'),
        ('多段速怎么配置？', '用数字输入端子组合选择段位，常见 3 个端子可组合 7 段速度。每段对应一个频率参数组，运行前确认端子功能码已映射到多段速选择。'),
        ('载波频率调高有什么影响？', '载波频率越高，电机电流波形越平滑、噪音越小，但开关损耗和驱动器温升上升。一般 4–8kHz 是平衡点，长电缆场合建议降到 2–4kHz。'),
        ('PID 参数怎么整定？', '先把积分和微分置零，只加比例直到出现小幅等幅振荡，再把比例降到 60%，然后加积分消除静差，最后少量加微分抑制超调。'),
    ],
    '安装接线': [
        ('主回路进线和出线怎么接？', '进线接 R/S/T（单相为 L/N），出线接 U/V/W 到电机，绝不能反接。PE 必须单独可靠接地，接地电阻小于 4Ω，且不要与焊机等大电流设备共用接地点。'),
        ('制动电阻怎么选？', '按制动功率和占空比选阻值与功率：阻值过小会过流，过大则制动不足。频繁启停场合按连续制动功率的 1.5 倍留余量，并保证电阻安装位置通风。'),
        ('不接制动电阻会怎样？', '减速时电机回馈能量无处释放，直流母线电压抬升，轻则报过压停机，重则炸电容。短减速时间或大惯量负载必须外接制动单元与电阻。'),
        ('现场干扰大怎么布线？', '动力线与信号线分槽走，平行间距大于 30cm，交叉时垂直交叉；屏蔽层单端接地，避免两端接地形成地环流。编码器线必须用双绞屏蔽线。'),
        ('控制端子接错会烧吗？', '把 24V 误接到模拟量输入或通讯口上通常会损坏接口芯片。接线前先断电，用万用表确认端子定义，首次上电建议只接电源与电机做空载测试。'),
    ],
    '报警处理': [
        ('报 OU 过压是什么原因？', '常见三种：减速时间设置过短、制动电阻未接或阻值偏大、电网电压本身偏高。先看报警时刻是否发生在减速阶段，再检查制动回路。'),
        ('报 OC 过流怎么查？', '先区分是加速中、恒速中还是减速中过流：加速中多为转矩提升不足或加速时间过短；恒速中多为负载突变或电机绝缘问题；减速中则看制动回路。'),
        ('报 OH 过热怎么办？', '检查风扇是否运转、散热片是否积灰、环境温度是否超过 40℃。载波频率设太高也会加剧发热，可先降到 4kHz 观察。'),
        ('报 GF 接地故障怎么查？', '断开电机电缆，用兆欧表测相间与相对地绝缘，低于 1MΩ 说明电机或电缆受潮受损。排除外部后再怀疑驱动器内部霍尔元件。'),
        ('上电就报对地短路？', '先拔掉电机线再上电：仍报错说明驱动器功率模块损坏；不报错说明外部电缆或电机对地短路。不要反复复位上电，可能扩大损坏范围。'),
        ('回原点冲过限位怎么办？', '原点开关信号抖动或减速距离不足。先降低回零速度，再确认原点信号滤波时间，必要时改为先找原点再回退一段的二次定位方式。'),
    ],
    '维护保养': [
        ('用久了要换什么？', '电解电容和散热风扇是寿命件：风扇一般 3–5 年更换，电容 5–8 年容量衰减明显。高温高湿环境按 60% 折算更换周期。'),
        ('风扇多久换一次？', '连续运行场合建议 3 年检查一次轴承间隙与转速，噪音明显变大或转速下降就更换。换风扇时注意风向，装反会导致热量排不出去。'),
        ('备件应该备哪些？', '同型号整机一台、风扇、控制板、端子台各一。换机前必须先导出参数并确认固件版本一致，不同版本直接替换可能参数不兼容。'),
        ('长期存放要注意什么？', '存放超过半年需每半年通电一次（约 30 分钟），给电解电容重新赋能。存放环境避免结露，湿度保持在 20%–80% 无凝露。'),
    ],
    '选型与规格': [
        ('能带多大功率的电机？', '按额定电流选而不是按功率选：驱动器额定输出电流需大于电机额定电流，重载启动或长电缆场合再留 20% 余量。'),
        ('内置制动单元吗？', '小功率机型通常内置制动单元但需外接制动电阻；中大功率机型多为外置。选型时确认制动单元是否内置，否则要单独订购。'),
        ('和上位机怎么选型？', '看三点：通讯协议是否支持（Modbus RTU / EtherCAT）、IO 点数是否够用、是否需要运动控制功能。协议不匹配时加网关会增加延迟。'),
        ('长期过载能力是多少？', '一般标注 150% 额定电流 60 秒、180% 3 秒。若工况是持续重载，应按 120% 连续选型，不要贴着过载曲线用。'),
    ],
    '通讯配置': [
        ('读写频率用哪个寄存器？', 'Modbus RTU 下频率给定与频率读取使用不同寄存器，注意区分「设定值」和「实际值」，并确认是 16 位还是 32 位、是否需要乘缩放系数。'),
        ('从站不识别怎么办？', '按顺序查：站号是否冲突、波特率与校验位是否一致、终端电阻是否接了、网线屏蔽是否接地。EtherCAT 还要看 ESI 文件版本是否匹配。'),
        ('通讯超时怎么排查？', '先降低通讯频率看是否还超时，再抓报文确认是主站没发还是从站没回。线长超过 50 米时要降波特率，并检查是否有节点掉电。'),
        ('多台并联怎么设站号？', '每台站号必须唯一，设置后需断电重启生效。批量调试建议先在工装台上逐台设好站号并贴标签，避免现场反复拆线。'),
    ],
}

ANSWER_TAIL = [
    '如果按上面的顺序排查仍无法解决，建议记录报警码、发生时刻和当时的负载情况，再做进一步判断。',
    '以上是通用排查思路，具体参数请以对应机型的说明书为准。',
    '现场处理前请先断电并确认母线电压已降到安全值。',
]


def build_rows(count=300):
    rnd = random.Random(20260101)
    rows = []
    used = set()
    combos = [(m, c, t) for m in MODELS for c in CATEGORIES for t in range(len(TOPICS[c]))]
    rnd.shuffle(combos)
    idx = 0
    for model, cat, tidx in combos:
        if len(rows) >= count:
            break
        topic, answer = TOPICS[cat][tidx]
        scene = rnd.choice(SCENES)
        question = f'{model} {topic[:-1]}（{scene}）' if topic.endswith('？') else f'{model} {topic}'
        if question in used:
            continue
        used.add(question)
        idx += 1
        rows.append({
            'display_no': idx,
            'question': question,
            'answer': answer + rnd.choice(ANSWER_TAIL),
            'category': cat,
            'approved': True,
            'public': True,
            'author': 'demo_employee',
            'tenant_id': db.DEFAULT_TENANT_ID,
        })
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reset', action='store_true', help='先清空默认租户的问答再灌')
    parser.add_argument('--count', type=int, default=300)
    args = parser.parse_args()

    database_url = os.environ.get('DATABASE_URL', 'postgresql://postgres@localhost:5432/knowledge_base')
    db.init_db({
        'DATABASE_URL': database_url,
        'DEFAULT_TENANT_ID': 1,
        'DEFAULT_TENANT_NAME': '演示公司',
        'DEFAULT_TENANT_SLUG': 'demo-company',
        'SUPERADMIN_USERNAME': os.environ.get('SUPERADMIN_USERNAME', 'superadmin'),
        'SUPERADMIN_PASSWORD': os.environ.get('SUPERADMIN_PASSWORD', ''),
        'SUPERADMIN_ROLE': 'superadmin',
    })
    db._init_app_data()

    conn = db._get_conn()
    if args.reset:
        conn.execute('DELETE FROM attachments WHERE tenant_id = %s', (db.DEFAULT_TENANT_ID,))
        conn.execute('DELETE FROM qa_pairs WHERE tenant_id = %s', (db.DEFAULT_TENANT_ID,))
        conn.execute("DELETE FROM meta WHERE key = 'next_qa_id'")
        conn.commit()
    conn.close()

    rows = build_rows(args.count)
    db.save_data({'qa_pairs': rows}, tenant_id=db.DEFAULT_TENANT_ID)
    print(f'[seed] 已写入 {len(rows)} 条演示问答（租户 {db.DEFAULT_TENANT_NAME}）')
    print('[seed] 账号：admin / demo-admin-2026，employee / demo-employee-2026，'
          'customer / demo-customer-2026（超级账号口令见控制台输出）')
    print('[seed] 首次启动 app.py 会自动建立向量索引（几百条约十几秒）')


if __name__ == '__main__':
    main()
