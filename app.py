import os
import re
import subprocess
import threading
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# 串行化 crontab 的读改写操作，防止并发请求相互覆盖
cron_lock = threading.Lock()

def run_cmd(cmd):
    """运行终端命令并返回输出"""
    try:
        res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        return res.stdout.strip()
    except Exception as e:
        return str(e)

def parse_cron_desc(line):
    """解析 cron 表达式为中文描述"""
    parts = line.split()
    if len(parts) < 6:
        return {"time": line, "command": line, "name": "未知任务"}
    minute, hour, dom, month, dow, cmd = parts[0], parts[1], parts[2], parts[3], parts[4], ' '.join(parts[5:])
    
    # 构建时间描述
    time_str = ""
    try:
        if minute == '*' and hour == '*':
            time_str = "每分钟"
        elif hour == '*':
            time_str = f"每小时第 {minute} 分"
        elif minute == '*':
            time_str = f"每小时"
        else:
            time_str = f"{int(hour):02d}:{int(minute):02d}"
    except:
        time_str = f"时间 {hour}:{minute}"
    
    # 频率描述
    freq = ""
    if dow != '*':
        dow_map = {'0':'周日','1':'周一','2':'周二','3':'周三','4':'周四','5':'周五','6':'周六'}
        if '-' in dow:
            start, end = dow.split('-')
            freq = f" 每周 {dow_map.get(start, start)}~{dow_map.get(end, end)}"
        else:
            freq = f" 每周{''.join([dow_map.get(d, d) for d in dow.split(',')])}"
    elif dom != '*':
        freq = f" 每月 {dom} 日"
    elif month != '*':
        freq = f" 每年 {int(month)} 月"
    else:
        freq = " 每天"
    
    # 获取命令简称
    cmd_short = cmd.split('/')[-1].split()[0] if cmd else ""
    # 提取脚本名或命令
    cmd_name = ""
    if '.py' in cmd or '.sh' in cmd:
        for p in cmd.split():
            if p.endswith('.py') or p.endswith('.sh'):
                cmd_name = os.path.basename(p).replace('.py','').replace('.sh','')
                break
    if not cmd_name:
        cmd_name = cmd_short
    
    return {"time": time_str + freq, "command": cmd, "name": cmd_name}

def get_user_cron_jobs():
    """获取用户 crontab 任务"""
    jobs = []
    try:
        raw = run_cmd("sudo crontab -l 2>/dev/null")
        for line in raw.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) >= 6 and parts[0].replace('*','').replace(',','').replace('-','').replace('/','').isdigit():
                desc = parse_cron_desc(line)
                jobs.append({
                    "id": f"cron_{len(jobs)}",
                    "type": "cron",
                    "enabled": True,
                    "schedule": line,
                    "time_desc": desc['time'],
                    "name": desc['name'],
                    "command": desc['command']
                })
    except:
        pass
    return jobs

def get_systemd_timers():
    """获取 systemd timer 任务"""
    timers = []
    try:
        raw = run_cmd("systemctl list-timers --all --no-pager --plain 2>/dev/null")
        for line in raw.split('\n'):
            if 'NEXT' in line or 'timers listed' in line or not line.strip():
                continue
            parts = line.split()
            if len(parts) >= 8:
                unit = parts[7] if len(parts) > 7 else ""
                if unit.endswith('.timer'):
                    svc = unit.replace('.timer', '.service')
                    active = run_cmd(f"systemctl is-active {unit}").strip() == "active"
                    timers.append({
                        "id": f"timer_{unit}",
                        "type": "timer",
                        "enabled": active,
                        "schedule": f"{parts[0]} ({parts[2]})",
                        "time_desc": parts[2] if len(parts) > 2 else "",
                        "name": svc.replace('.service',''),
                        "command": svc
                    })
    except:
        pass
    return timers

def toggle_cron_job(job_id, enable):
    """开启/关闭 cron 任务"""
    try:
        with cron_lock:
            raw = run_cmd("sudo crontab -l 2>/dev/null")
            lines = raw.split('\n')
            idx = int(job_id.replace('cron_', ''))
            count = 0
            for i, line in enumerate(lines):
                line_stripped = line.strip()
                if line_stripped and not line_stripped.startswith('#'):
                    parts = line_stripped.split()
                    if len(parts) >= 6 and parts[0].replace('*','').replace(',','').replace('-','').replace('/','').isdigit():
                        if count == idx:
                            if enable and line_stripped.startswith('#'):
                                lines[i] = line_stripped[1:].strip()
                            elif not enable and not line_stripped.startswith('#'):
                                lines[i] = '# ' + line_stripped
                            break
                        count += 1
            new_cron = '\n'.join(lines) + '\n'
            proc = subprocess.Popen(['sudo', 'crontab', '-'], stdin=subprocess.PIPE, text=True)
            proc.communicate(input=new_cron)
        return True
    except:
        return False

def toggle_systemd_timer(unit, enable):
    """开启/关闭 systemd timer"""
    try:
        action = "enable" if enable else "disable"
        subprocess.run(["sudo", "systemctl", action, "--now", unit], check=True)
        return True
    except:
        return False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/cron/list')
def cron_list():
    cron_jobs = get_user_cron_jobs()
    timer_jobs = get_systemd_timers()
    return jsonify({
        "cron": cron_jobs,
        "timers": timer_jobs,
        "total": len(cron_jobs) + len(timer_jobs)
    })

@app.route('/api/cron/toggle', methods=['POST'])
def cron_toggle():
    d = request.json
    job_id = d.get('id', '')
    enable = d.get('enable', True)
    
    if job_id.startswith('cron_') and job_id.replace('cron_', '').isdigit():
        success = toggle_cron_job(job_id, enable)
    elif job_id.startswith('timer_'):
        unit = job_id.replace('timer_', '')
        if re.match(r'^[a-zA-Z0-9.-]+\.timer$', unit):
            success = toggle_systemd_timer(unit, enable)
        else:
            return jsonify({"success": False, "msg": "Invalid timer name"})
    else:
        return jsonify({"success": False, "msg": "Unknown job type"})
    
    return jsonify({"success": success, "msg": "OK" if success else "Failed"})

@app.route('/api/cron/run_once', methods=['POST'])
def cron_run_once():
    d = request.json
    cmd = d.get('command')
    if not cmd:
        return jsonify({"success": False, "msg": "未接收到指令内容"})
    try:
        def run_bg(c):
            subprocess.run(c, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=run_bg, args=(cmd,), daemon=True).start()
        return jsonify({"success": True, "msg": "指令已在系统后台异步触发"})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})

@app.route('/api/cron/add', methods=['POST'])
def cron_add():
    d = request.json
    schedule = d.get('schedule')
    cmd = d.get('command')
    name = d.get('name', '')
    if not schedule or not cmd:
        return jsonify({"success": False, "msg": "时间表达式与执行指令不能为空"})
    # 拒绝换行符，防止借单条记录注入多条 crontab 条目
    if '\n' in schedule or '\r' in schedule or '\n' in cmd or '\r' in cmd or '\n' in name or '\r' in name:
        return jsonify({"success": False, "msg": "内容不能包含换行符"})
    try:
        if len(schedule.split()) < 5:
            return jsonify({"success": False, "msg": "Cron 时间表达式格式无效"})
        with cron_lock:
            raw = run_cmd("sudo crontab -l 2>/dev/null")
            lines = raw.split('\n') if raw else []
            if lines and not lines[-1].strip():
                lines.pop()
            if name:
                lines.append(f"# {name}")
            lines.append(f"{schedule} {cmd}")
            new_cron = '\n'.join(lines) + '\n'
            proc = subprocess.Popen(['sudo', 'crontab', '-'], stdin=subprocess.PIPE, text=True)
            proc.communicate(input=new_cron)
        return jsonify({"success": True, "msg": "新定时任务添加成功"})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})

@app.route('/api/cron/delete', methods=['POST'])
def cron_delete():
    d = request.json
    job_id = d.get('id', '')
    if not job_id.startswith('cron_') or not job_id.replace('cron_', '').isdigit():
        return jsonify({"success": False, "msg": "无效的任务 ID"})
    try:
        with cron_lock:
            raw = run_cmd("sudo crontab -l 2>/dev/null")
            lines = raw.split('\n')
            idx = int(job_id.replace('cron_', ''))
            count = 0
            target_line_idx = -1
            for i, line in enumerate(lines):
                line_stripped = line.strip()
                if line_stripped and not line_stripped.startswith('#'):
                    parts = line_stripped.split()
                    if len(parts) >= 6 and parts[0].replace('*','').replace(',','').replace('-','').replace('/','').isdigit():
                        if count == idx:
                            target_line_idx = i
                            break
                        count += 1
            if target_line_idx != -1:
                lines.pop(target_line_idx)
                if target_line_idx > 0 and lines[target_line_idx-1].strip().startswith('#'):
                    lines.pop(target_line_idx-1)
                new_cron = '\n'.join(lines) + '\n'
                proc = subprocess.Popen(['sudo', 'crontab', '-'], stdin=subprocess.PIPE, text=True)
                proc.communicate(input=new_cron)
            else:
                return jsonify({"success": False, "msg": "未在 Crontab 中定位到该任务"})
        return jsonify({"success": True, "msg": "任务已成功删除"})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
