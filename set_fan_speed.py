#!/usr/bin/python
# -*- coding: UTF-8 -*-
#
# 运行此脚本前，请确保已安装所需库:
# pip install requests pycryptodome
#
import requests
import socket
import time
import base64
from Crypto.Cipher import Blowfish
from Crypto.Util.Padding import pad
import urllib3

# 禁用 InsecureRequestWarning 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 配置信息 ---
dic = {
    'username': 'zlkj',
    'password': 'Zl123456.',
    'ip': '192.168.20.11',
    'speed': '1',  # 需要调整的转速百分比
    'fans': ["1", "3", "5", "7"]  # 需要调整转速的风扇端口 (0-7)
}

# --- 加密算法实现 ---

def encry_str(s: str) -> str:
    """
    使用XOR 127和十六进制编码来混淆字符串，模拟JavaScript中的encryStr函数。
    """
    if not s:
        return ""
    return '-'.join([hex(ord(char) ^ 127)[2:] for char in s])

def encrypt_blowfish(text: str, key: str) -> str:
    """
    使用Blowfish ECB模式加密文本，然后进行Base64编码。
    """
    cipher = Blowfish.new(key.encode('utf-8'), Blowfish.MODE_ECB)
    padded_text = pad(text.encode('utf-8'), Blowfish.block_size)
    encrypted_text = cipher.encrypt(padded_text)
    return base64.b64encode(encrypted_text).decode('utf-8')

# --- 核心功能 ---

def is_port_open(ip_address, port):
    """检查指定IP的端口是否开放"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    result = sock.connect_ex((ip_address, port))
    sock.close()
    return result == 0

def login(ip, username, password):
    """
    执行登录流程，返回一个包含认证cookie和CSRF令牌的requests.Session对象。
    """
    print("开始登录 bmc ------>")
    base_url = f"https://{ip}"
    session = requests.Session()
    # 添加 'X-Requested-With' 头，模拟AJAX请求
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
        'Referer': f"{base_url}/main.html",
        'X-Requested-With': 'XMLHttpRequest'
    })

    # 1. 获取加密模式和登录标签
    try:
        print("步骤 1/2: 获取加密模式...")
        random_tag_resp = session.get(f"{base_url}/api/randomtag", timeout=10, verify=False)
        random_tag_resp.raise_for_status()
        login_data = random_tag_resp.json()
        encrypt_ctrl = login_data.get('encrypt_ctrl', 0)
        login_tag = login_data.get('random')
        print(f"获取成功, 加密模式 (encrypt_ctrl): {encrypt_ctrl}")
    except requests.RequestException as e:
        print(f"获取加密模式失败: {e}")
        return None

    # 2. 根据加密模式加密凭据并登录
    encrypt_username = username
    encrypt_password = password

    if encrypt_ctrl == 1:
        print("使用 XOR 加密...")
        encrypt_username = encry_str(username)
        encrypt_password = encry_str(password)
    elif encrypt_ctrl == 2:
        print("使用 Blowfish 加密...")
        secret_key = "secret"
        encrypt_username = encrypt_blowfish(username, secret_key)
        encrypt_password = encrypt_blowfish(password, secret_key)
    else:
        print("使用明文...")

    login_payload = {
        'username': encrypt_username,
        'password': encrypt_password,
        'encrypt_flag': encrypt_ctrl,
        'login_tag': login_tag
    }

    try:
        print("步骤 2/2: 发送登录请求...")
        login_resp = session.post(f"{base_url}/api/session", data=login_payload, timeout=10, verify=False)
        login_resp.raise_for_status()
        login_result = login_resp.json()

        if login_result.get('ok') == 0 and 'CSRFToken' in login_result:
            csrf_token = login_result['CSRFToken']
            session.headers.update({'X-CSRFTOKEN': csrf_token})
            print(f"登录成功! CSRF Token已设置。")
            print('——————————————————————————————————————————————————————————————')
            return session
        else:
            error_msg = login_result.get('error_msg', '未知错误')
            if 'CSRFToken' not in login_result:
                error_msg = "登录响应中未找到 CSRFToken。"
            print(f"登录失败: {error_msg}")
            return None

    except requests.RequestException as e:
        print(f"登录请求失败: {e}")
        return None

def get_fan_mode(session, ip):
    """获取当前风扇控制模式"""
    print("正在获取当前风扇模式...")
    url = f"https://{ip}/api/settings/fans-mode"
    try:
        response = session.get(url, timeout=10, verify=False)
        response.raise_for_status()
        mode = response.json().get("control_mode", "未知")
        print(f"获取成功, 当前风扇模式为: {mode}")
        return mode
    except requests.RequestException as e:
        print(f"获取风扇模式失败: {e}")
        return None

def set_fan_mode(session, ip, mode):
    """设置风扇控制模式 ('manual' 或 'auto')"""
    print(f"开始设置风扇模式为: {mode}...")
    url = f"https://{ip}/api/settings/fans-mode"
    payload = {'control_mode': mode}
    try:
        response = session.post(url, json=payload, timeout=10, verify=False)
        response.raise_for_status()
        print(f"已成功发送设置请求，目标模式: {mode}")
        print('——————————————————————————————————————————————————————————————')
        return True
    except requests.RequestException as e:
        print(f"设置风扇模式失败: {e}")
        return False

def set_fan_speed(session, ip, fan_id, speed_percent):
    """设置单个风扇的速度"""
    print(f"开始调整风扇 {fan_id} 的速度为 {speed_percent}%")
    url = f"https://{ip}/api/settings/fan/{fan_id}"
    payload = {'duty': speed_percent}
    try:
        response = session.put(url, json=payload, timeout=10, verify=False)
        response.raise_for_status()
        print(f"调整风扇 {fan_id} 完成。")
        return True
    except requests.RequestException as e:
        error_message = f"调整风扇 {fan_id} 失败: {e}"
        if e.response is not None:
            error_message += f"\n - Status Code: {e.response.status_code}"
            error_message += f"\n - Response Body: {e.response.text}"
        print(error_message)
        return False

def get_fan_info(session, ip):
    """获取所有风扇的详细信息"""
    print("正在获取所有风扇的当前状态...")
    url = f"https://{ip}/api/status/fan_info"
    try:
        response = session.get(url, timeout=10, verify=False)
        response.raise_for_status()
        fan_info = response.json()
        print("获取风扇状态成功。")
        return fan_info.get("fans", [])
    except (requests.RequestException, ValueError) as e:
        print(f"获取风扇状态失败: {e}")
        return None

# --- 主程序 ---
if __name__ == "__main__":
    print("感谢使用修改风扇速度脚本")
    print(f'目标IP: {dic["ip"]}')
    print(f'用户名: {dic["username"]}')
    print(f'待调整速度: {dic["speed"]}%')
    print('——————————————————————————————————————————————————————————————')

    # 检查端口连通性
    print("正在测试BMC控制台端口连通性...")
    if not is_port_open(dic['ip'], 443):
        print(f"无法连接到 {dic['ip']}:443 (HTTPS)。请检查网络连接或IP地址。")
        exit()
    print("端口测试通过。")
    print('——————————————————————————————————————————————————————————————')

    # 登录
    auth_session = login(dic['ip'], dic['username'], dic['password'])

    if not auth_session:
        print("无法完成登录，脚本退出。")
        exit()

    # 获取当前模式
    current_mode = get_fan_mode(auth_session, dic['ip'])
    print('——————————————————————————————————————————————————————————————')

    if current_mode == 'manual':
        print("风扇已处于手动模式，无需再次设置。")
    else:
        # 设置为手动模式
        if not set_fan_mode(auth_session, dic['ip'], 'manual'):
            print("无法设置风扇为手动模式，脚本退出。")
            exit()

        # 再次获取模式以确认更改
        print("等待2秒后确认模式...")
        time.sleep(2)
        get_fan_mode(auth_session, dic['ip'])
    print('——————————————————————————————————————————————————————————————')

    # 获取所有风扇的当前状态
    all_fans_info = get_fan_info(auth_session, dic['ip'])
    if all_fans_info is None:
        print("无法获取风扇信息，脚本退出。")
        exit()

    # 将风扇列表转换为以ID为键的字典，方便快速查找
    # 注意：JSON中的id是数字，而配置中的是字符串
    fan_status_map = {str(fan['id']): fan for fan in all_fans_info}

    # 调整所有指定风扇的速度
    print('开始检查并调整所有指定风扇的风速...')
    all_success = True
    # 将配置中的速度转换为整数以便比较
    target_speed = int(dic['speed'])

    for fan_id_str in dic['fans']:
        if fan_id_str not in fan_status_map:
            print(f"警告: 在风扇信息中未找到ID为 {fan_id_str} 的风扇，跳过调整。")
            continue

        current_fan = fan_status_map[fan_id_str]
        current_speed = current_fan.get('speed_percent')

        if current_speed is None:
            print(f"警告: 无法获取风扇 {fan_id_str} 的当前速度，跳过调整。")
            continue

        print(f"检查风扇 {fan_id_str}: 当前速度 {current_speed}%, 目标速度 {target_speed}%")

        if current_speed == target_speed:
            print(f"风扇 {fan_id_str} 的速度已是 {target_speed}%, 无需调整。")
        else:
            # 调用设置函数时，仍然使用配置中的原始字符串格式的速度值
            if not set_fan_speed(auth_session, dic['ip'], fan_id_str, dic['speed']):
                all_success = False
        print('---')  # 为每个风扇的处理添加分隔符，使输出更清晰

    print('——————————————————————————————————————————————————————————————')

    if all_success:
        print('所有风扇速度调整任务已成功完成。')
    else:
        print('部分风扇速度调整失败，请检查上面的日志。')

    print('脚本执行完毕，退出。')
    exit()
