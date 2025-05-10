from flask import Flask, request, redirect, url_for, render_template, session, flash, send_from_directory, abort, jsonify
import pymysql
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import csv
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
from datetime import datetime

# 初始化 Flask 应用
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'eden')  # 建议生产环境用环境变量

# 初始化 SocketIO
socketio = SocketIO(app, manage_session=False)

# MySQL 数据库配置
db_config = {
    'host': 'localhost',
    'user': 'root',
    'password': '31415926ab',
    'db': 'game_resources',
    'charset': 'utf8mb4'
}

# 在线用户集合
online_users = set()

# 静态与上传目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESOURCE_FOLDER = os.path.join(BASE_DIR, 'resources')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif'}
PER_PAGE = 10

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.',1)[1].lower() in ALLOWED_EXT

# 数据库初始化：创建 chat_messages 表
def init_db():
    conn = pymysql.connect(**db_config)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_messages (
        id INT AUTO_INCREMENT PRIMARY KEY,
        sender VARCHAR(255) NOT NULL,
        receiver VARCHAR(255) DEFAULT '',
        message TEXT,
        image_url VARCHAR(512),
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    ) CHARACTER SET utf8mb4;
    """)
    conn.commit()
    conn.close()

# ----------------- 普通路由 -----------------

@app.before_request
def require_login():
    if request.endpoint in ['login', 'register', 'static']:
        return None
    if 'user_id' not in session:
        return redirect(url_for('login'))

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method=='POST':
        username = request.form.get('username','').strip()
        password = request.form.get('password','').strip()
        if not username or not password:
            flash("用户名和密码不能为空")
            return render_template('register.html')
        try:
            conn = pymysql.connect(**db_config)
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE username=%s", (username,))
            if cur.fetchone():
                flash("用户名已存在")
                conn.close()
                return render_template('register.html')
            pwd_hash = generate_password_hash(password)
            cur.execute("INSERT INTO users(username,password_hash) VALUES(%s,%s)", (username, pwd_hash))
            conn.commit()
            conn.close()
        except Exception:
            flash("注册失败，请稍后再试")
            return render_template('register.html')
        flash("注册成功，请登录")
        return redirect(url_for('login'))
        # ——处理头像文件——
        avatar_url = '/static/images/default_avatar.png'
        if 'avatar' in request.files:
            f = request.files['avatar']
            if f and allowed_file(f.filename):
                fn = secure_filename(f.filename)
                save_path = os.path.join(BASE_DIR, 'static', 'avatars', fn)
                f.save(save_path)
                avatar_url = url_for('static', filename='avatars/' + fn)
        # 插入用户时一并写入 avatar_url
        cur.execute(
           "INSERT INTO users(username,password_hash,avatar_url) VALUES(%s,%s,%s)",
           (username, pwd_hash, avatar_url)
        )
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        # 1. 单独 try/except 只捕获“查询”阶段的数据库异常
        try:
            conn = pymysql.connect(**db_config)
            cur = conn.cursor(pymysql.cursors.DictCursor)
            cur.execute(
                "SELECT id, username, password_hash, avatar_url "
                "FROM users WHERE username = %s",
                (username,)
            )
            user = cur.fetchone()
        except Exception as e:
            # 只处理连接或查询出错
            app.logger.error("登录时数据库查询出错：%s", e)
            flash("数据库错误，请稍后再试", "danger")
            return render_template('login.html')
        finally:
            conn.close()

        # 2. 验证用户存在且密码正确
        if not user or not check_password_hash(user['password_hash'], password):
            flash("用户名或密码不正确", "warning")
            return render_template('login.html')

        # 3. 登录成功——这里不再在 try 里面取 avatar_url，避免 KeyError 被当成 DB 错误
        session['user_id']   = user['id']
        session['username']  = user['username']
        # 用 get() + or 确保即便为空也会回退到默认头像
        session['avatar_url'] = user.get('avatar_url') or url_for(
            'static', filename='images/default_avatar.png'
        )

        return redirect(url_for('resources'))

    # GET 请求直接渲染登录页
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/resources')
def resources():
    category = request.args.get('category')
    search_q = request.args.get('q')
    try: page = int(request.args.get('page',1))
    except: page = 1
    if page<1: page=1

    base_sql = "SELECT id,name,category,download_count FROM resources"
    count_sql = "SELECT COUNT(*) FROM resources"
    conds, params = [], []
    if category:
        conds.append("category=%s"); params.append(category)
    if search_q:
        conds.append("name LIKE %s"); params.append(f"%{search_q}%")
    if conds:
        where = " WHERE " + " AND ".join(conds)
        base_sql += where; count_sql += where

    base_sql += " ORDER BY category,name LIMIT %s OFFSET %s"
    offset = (page-1)*PER_PAGE
    params.extend([PER_PAGE, offset])

    resources_list = []; total_count=0
    try:
        conn = pymysql.connect(**db_config)
        cur = conn.cursor(pymysql.cursors.DictCursor)
        if conds:
            cur.execute(count_sql, tuple(params[:-2]))
        else:
            cur.execute(count_sql)
        total_count = cur.fetchone()['COUNT(*)']
        cur.execute(base_sql, tuple(params))
        resources_list = cur.fetchall()
        conn.close()
    except Exception:
        flash("数据库查询出错")

    total_pages = max((total_count+PER_PAGE-1)//PER_PAGE,1)
    if page>total_pages: page=total_pages

    return render_template('resources.html',
                           resources=resources_list,
                           category=category,
                           page=page,
                           total_pages=total_pages)

@app.route('/search')
def search():
    q = request.args.get('q','').strip()
    try: page=int(request.args.get('page',1))
    except: page=1
    resources_list=[]; total_count=0
    if q:
        cond = "WHERE name LIKE %s"
        count_sql = f"SELECT COUNT(*) FROM resources {cond}"
        sql = f"SELECT id,name,category,download_count FROM resources {cond} ORDER BY category,name LIMIT %s OFFSET %s"
        offset=(page-1)*PER_PAGE
        try:
            conn = pymysql.connect(**db_config)
            cur = conn.cursor(pymysql.cursors.DictCursor)
            cur.execute(count_sql, (f"%{q}%",))
            total_count = cur.fetchone()['COUNT(*)']
            cur.execute(sql, (f"%{q}%", PER_PAGE, offset))
            resources_list = cur.fetchall()
            conn.close()
        except Exception:
            flash("搜索出错，请稍后再试")
    else:
        flash("请输入搜索关键词")
    total_pages = max((total_count+PER_PAGE-1)//PER_PAGE,1)
    if page>total_pages: page=total_pages
    return render_template('search_results.html',
                           resources=resources_list,
                           q=q,
                           page=page,
                           total_pages=total_pages,
                           total_count=total_count)

@app.route('/download/<int:res_id>')
def download(res_id):
    try:
        conn = pymysql.connect(**db_config)
        cur = conn.cursor(pymysql.cursors.DictCursor)
        cur.execute("SELECT filename,download_count FROM resources WHERE id=%s",(res_id,))
        res = cur.fetchone()
        if not res:
            conn.close(); abort(404)
        cur.execute("UPDATE resources SET download_count=download_count+1 WHERE id=%s",(res_id,))
        conn.commit(); conn.close()
    except Exception:
        abort(500)
    return send_from_directory(RESOURCE_FOLDER, res['filename'], as_attachment=True)

@app.route('/comment/<int:res_id>', methods=['GET','POST'])
def comment(res_id):
    if 'user_id' not in session:
        flash("请先登录后再发表评论"); return redirect(url_for('login'))
    if request.method=='POST':
        txt = request.form.get('comment','').strip()
        if not txt:
            flash("评论不能为空","warning"); return redirect(url_for('comment',res_id=res_id))
        csv_file = os.path.join(BASE_DIR,"note.csv")
        exists = os.path.exists(csv_file)
        with open(csv_file,"a",newline="",encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["res_id","username","comment","timestamp"])
            if not exists or os.stat(csv_file).st_size==0:
                writer.writeheader()
            writer.writerow({
                "res_id":res_id,
                "username":session.get('username','匿名'),
                "comment":txt,
                "timestamp":datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
        flash("评论已提交","success")
        return redirect(url_for('comments',res_id=res_id))
    return render_template("comment.html", res_id=res_id)

@app.route('/comments/<int:res_id>')
def comments(res_id):
    comments_list=[]
    csv_file = os.path.join(BASE_DIR,"note.csv")
    if os.path.exists(csv_file):
        with open(csv_file,"r",encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    if int(r["res_id"])==res_id:
                        comments_list.append(r)
                except:
                    pass
    return render_template("comments.html", comments=comments_list, res_id=res_id)

@app.route('/upload_image', methods=['POST'])
def upload_image():
    if 'file' not in request.files:
        return {'error':'未检测到文件'},400
    f = request.files['file']
    if f.filename=='' or not allowed_file(f.filename):
        return {'error':'文件类型不支持'},400
    fn = secure_filename(f.filename)
    path = os.path.join(UPLOAD_FOLDER, fn)
    f.save(path)
    url = url_for('static',filename='uploads/'+fn)
    return {'url':url}

# ----------------- 聊天 API & SocketIO -----------------

@app.route('/api/history')
def api_history():
    # 当前登录用户名
    current_user = session.get('username', '')
    # 如果有传 receiver，则为私聊模式
    receiver = request.args.get('receiver', '').strip()

    try:
        conn = pymysql.connect(**db_config)
        cur = conn.cursor(pymysql.cursors.DictCursor)

        if receiver:
            # 私聊：查询双方互发消息，并关联 users 表取 avatar_url
            sql = """
                SELECT 
                    m.sender    AS username,
                    u.avatar_url,
                    m.message,
                    m.image_url,
                    m.timestamp
                FROM chat_messages m
                LEFT JOIN users u ON u.username = m.sender
                WHERE (m.sender=%s AND m.receiver=%s)
                   OR (m.sender=%s AND m.receiver=%s)
                ORDER BY m.timestamp ASC
            """
            params = (current_user, receiver, receiver, current_user)
        else:
            # 群聊：只查询 receiver 为空的消息
            sql = """
                SELECT 
                    m.sender    AS username,
                    u.avatar_url,
                    m.message,
                    m.image_url,
                    m.timestamp
                FROM chat_messages m
                LEFT JOIN users u ON u.username = m.sender
                WHERE m.receiver = '' OR m.receiver IS NULL
                ORDER BY m.timestamp ASC
            """
            params = ()

        cur.execute(sql, params)
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        print("查询历史出错：", e)
        return jsonify([])  # 出错时返回空数组

    # 将 datetime 转为字符串，方便前端直接显示
    for row in rows:
        if isinstance(row['timestamp'], datetime):
            row['timestamp'] = row['timestamp'].strftime("%Y-%m-%d %H:%M:%S")

    # 返回 JSON 列表，每条记录中包含：username, avatar_url, message, image_url, timestamp
    return jsonify(rows)

@app.route('/api/users')
def api_users():
    return jsonify(sorted(list(online_users)))

@socketio.on('connect')
def sock_connect():
    user = session.get('username')
    if user:
        join_room(user)
        online_users.add(user)

@socketio.on('disconnect')
def sock_disconnect():
    user = session.get('username')
    if user and user in online_users:
        online_users.remove(user)

@socketio.on('send_message')
def handle_message(data):
    sender = session.get('username','匿名')
    message = data.get('message','')
    image_url = data.get('image_url','')
    recv = data.get('receiver','')
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 写入数据库
    try:
        conn = pymysql.connect(**db_config)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages(sender, receiver, message, image_url, timestamp) VALUES(%s,%s,%s,%s,%s)",
            (sender, recv, message, image_url, ts)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("插入聊天记录出错：", e)

    payload = {'username': sender, 'message': message, 'image_url': image_url, 'timestamp': ts}

    if recv:
        # 私聊：发给接收者和发送者各自房间
        emit('receive_message', payload, room=recv)
        emit('receive_message', payload, room=sender)
    else:
        # 群聊：广播
        emit('receive_message', payload, broadcast=True)

@app.route('/chatroom')
def chatroom():
    return render_template('chatroom.html', username=session.get('username','匿名'))

if __name__ == '__main__':
    init_db()
    socketio.run(app, host='0.0.0.0', port=7207)
