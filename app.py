import csv
import hashlib
import html
import io
import os
import uuid
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from functools import wraps

import bleach
import markdown
from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, event, func
from sqlalchemy.engine import Engine
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
COVERS_DIR = os.path.join(DATA_DIR, "covers")
os.makedirs(COVERS_DIR, exist_ok=True)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def utc_now():
    """Возвращает текущее UTC-время в naive-формате для хранения в SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_moscow_time(value):
    """Переводит дату из UTC, как она хранится в БД, в московское время."""
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW_TZ)


def moscow_now_date():
    return datetime.now(MOSCOW_TZ).date()


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'library.sqlite')}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = COVERS_DIR

# Для Railway/Render иногда DATABASE_URL приходит как postgres://, SQLAlchemy ждёт postgresql://.
if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgres://"):
    app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"].replace(
        "postgres://", "postgresql://", 1
    )


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    """Включает ON DELETE CASCADE для SQLite."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
    cursor.close()


db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Для выполнения данного действия необходимо пройти процедуру аутентификации"
login_manager.login_message_category = "warning"

book_genres = db.Table(
    "book_genres",
    db.Column("book_id", db.Integer, db.ForeignKey("books.id", ondelete="CASCADE"), primary_key=True),
    db.Column("genre_id", db.Integer, db.ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True),
)


class Role(db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.Text, nullable=False)

    users = db.relationship("User", back_populates="role")


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    login = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    last_name = db.Column(db.String(80), nullable=False)
    first_name = db.Column(db.String(80), nullable=False)
    middle_name = db.Column(db.String(80), nullable=True)
    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)

    role = db.relationship("Role", back_populates="users")
    reviews = db.relationship("Review", back_populates="user", cascade="all, delete-orphan")
    visits = db.relationship("BookVisit", back_populates="user")

    @property
    def full_name(self):
        parts = [self.last_name, self.first_name, self.middle_name]
        return " ".join(part for part in parts if part)

    @property
    def role_name(self):
        return self.role.name if self.role else ""


class Genre(db.Model):
    __tablename__ = "genres"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)


class Book(db.Model):
    __tablename__ = "books"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    publisher = db.Column(db.String(255), nullable=False)
    author = db.Column(db.String(255), nullable=False)
    pages = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    genres = db.relationship("Genre", secondary=book_genres, backref=db.backref("books", lazy="dynamic"))
    cover = db.relationship("Cover", back_populates="book", uselist=False, cascade="all, delete-orphan")
    reviews = db.relationship("Review", back_populates="book", cascade="all, delete-orphan")
    visits = db.relationship("BookVisit", back_populates="book", cascade="all, delete-orphan")

    @property
    def genres_text(self):
        return ", ".join(genre.name for genre in self.genres)


class Cover(db.Model):
    __tablename__ = "covers"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(100), nullable=False)
    md5_hash = db.Column(db.String(32), nullable=False, index=True)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id", ondelete="CASCADE"), nullable=False, unique=True)

    book = db.relationship("Book", back_populates="cover")


class Review(db.Model):
    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("book_id", "user_id", name="uq_review_book_user"),)

    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)

    book = db.relationship("Book", back_populates="reviews")
    user = db.relationship("User", back_populates="reviews")


class BookVisit(db.Model):
    __tablename__ = "book_visits"

    id = db.Column(db.Integer, primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    visitor_id = db.Column(db.String(36), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now, index=True)

    book = db.relationship("Book", back_populates="visits")
    user = db.relationship("User", back_populates="visits")


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


ALLOWED_HTML_TAGS = set(bleach.sanitizer.ALLOWED_TAGS).union(
    {
        "p",
        "br",
        "ul",
        "ol",
        "li",
        "strong",
        "em",
        "blockquote",
        "code",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
    }
)
ALLOWED_HTML_ATTRIBUTES = {
    "a": ["href", "title", "rel", "target"],
    "th": ["align"],
    "td": ["align"],
}


@app.template_filter("markdown")
def markdown_filter(text):
    html = markdown.markdown(text or "", extensions=["extra", "nl2br"])
    clean_html = bleach.clean(
        html,
        tags=ALLOWED_HTML_TAGS,
        attributes=ALLOWED_HTML_ATTRIBUTES,
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    return bleach.linkify(clean_html)


@app.template_filter("dt")
def datetime_filter(value):
    msk_value = as_moscow_time(value)
    if not msk_value:
        return ""
    return msk_value.strftime("%d.%m.%Y %H:%M МСК")


@app.context_processor
def inject_helpers():
    return {
        "now_year": datetime.now(MOSCOW_TZ).year,
        "can_add_books": current_user.is_authenticated and current_user.role_name == "administrator",
        "can_edit_books": current_user.is_authenticated
        and current_user.role_name in {"administrator", "moderator"},
        "can_delete_books": current_user.is_authenticated and current_user.role_name == "administrator",
        "is_admin": current_user.is_authenticated and current_user.role_name == "administrator",
    }


def clean_markdown_source(text):
    """цдаляет потенциально опасные HTML теги, но оставляет markdown разметку."""
    return bleach.clean(text or "", tags=[], attributes={}, strip=True).strip()


def roles_required(*role_names):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                flash("Для выполнения данного действия необходимо пройти процедуру аутентификации", "warning")
                return redirect(url_for("login", next=request.full_path))
            if current_user.role_name not in role_names:
                flash("У вас недостаточно прав для выполнения данного действия!", "danger")
                return redirect(url_for("index"))
            return view_func(*args, **kwargs)

        return wrapper

    return decorator


def get_visitor_id(create=False):
    if "visitor_id" not in session and create:
        session["visitor_id"] = str(uuid.uuid4())
    return session.get("visitor_id")


def record_book_visit(book_id):
    """записывает просмотр книги с ограничением. максимум 10 раз в день по МСК."""
    now = utc_now()
    now_msk = as_moscow_time(now)
    day_start_msk = datetime.combine(now_msk.date(), time.min, tzinfo=MOSCOW_TZ)
    day_start = day_start_msk.astimezone(timezone.utc).replace(tzinfo=None)
    day_end = (day_start_msk + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)

    query = BookVisit.query.filter(
        BookVisit.book_id == book_id,
        BookVisit.created_at >= day_start,
        BookVisit.created_at < day_end,
    )

    if current_user.is_authenticated:
        query = query.filter(BookVisit.user_id == current_user.id)
        visitor_id = get_visitor_id(create=False)
        user_id = current_user.id
    else:
        visitor_id = get_visitor_id(create=True)
        query = query.filter(BookVisit.visitor_id == visitor_id, BookVisit.user_id.is_(None))
        user_id = None

    if query.count() >= 10:
        return

    visit = BookVisit(book_id=book_id, user_id=user_id, visitor_id=visitor_id, created_at=now)
    db.session.add(visit)
    db.session.commit()


def get_book_review_stats(book_ids):
    if not book_ids:
        return {}
    rows = (
        db.session.query(Review.book_id, func.avg(Review.rating), func.count(Review.id))
        .filter(Review.book_id.in_(book_ids))
        .group_by(Review.book_id)
        .all()
    )
    return {
        book_id: {"avg": round(avg_rating, 1) if avg_rating is not None else None, "count": review_count}
        for book_id, avg_rating, review_count in rows
    }


def get_popular_books(limit=5):
    since = utc_now() - timedelta(days=90)
    return (
        db.session.query(Book, func.count(BookVisit.id).label("views_count"))
        .join(BookVisit)
        .filter(BookVisit.created_at >= since)
        .group_by(Book.id)
        .order_by(func.count(BookVisit.id).desc(), Book.title.asc())
        .limit(limit)
        .all()
    )


def get_recent_books(limit=5):
    query = db.session.query(Book, func.max(BookVisit.created_at).label("last_seen")).join(BookVisit)
    if current_user.is_authenticated:
        query = query.filter(BookVisit.user_id == current_user.id)
    else:
        visitor_id = get_visitor_id(create=False)
        if not visitor_id:
            return []
        query = query.filter(BookVisit.visitor_id == visitor_id, BookVisit.user_id.is_(None))

    return (
        query.group_by(Book.id)
        .order_by(func.max(BookVisit.created_at).desc())
        .limit(limit)
        .all()
    )


def make_pagination(page, total_items, per_page):
    total_pages = max((total_items + per_page - 1) // per_page, 1)
    page = max(min(page, total_pages), 1)
    return {"page": page, "total_pages": total_pages, "has_prev": page > 1, "has_next": page < total_pages}


def parse_positive_int(value, field_name, min_value=1, max_value=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Поле «{field_name}» должно быть числом")
    if number < min_value:
        raise ValueError(f"Поле «{field_name}» должно быть не меньше {min_value}")
    if max_value is not None and number > max_value:
        raise ValueError(f"Поле «{field_name}» должно быть не больше {max_value}")
    return number


def allowed_cover_file(filename):
    allowed_extensions = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions


def build_svg_cover(book, color_seed=None):
    """Создаёт простую SVG-обложку, если пользователь не загрузил файл."""
    color_seed = color_seed if color_seed is not None else (book.id or 1)
    title = html.escape(book.title or "Без названия")
    author = html.escape(book.author or "")
    year = html.escape(str(book.year or ""))
    return f"""<svg xmlns='http://www.w3.org/2000/svg' width='480' height='720' viewBox='0 0 480 720'>
  <rect width='480' height='720' fill='hsl({color_seed * 31 % 360}, 62%, 38%)'/>
  <rect x='34' y='38' width='412' height='644' rx='26' fill='rgba(255,255,255,0.12)' stroke='rgba(255,255,255,0.50)' stroke-width='4'/>
  <text x='240' y='170' text-anchor='middle' font-family='Arial, sans-serif' font-size='24' fill='rgba(255,255,255,0.85)'>{author}</text>
  <foreignObject x='58' y='225' width='364' height='230'>
    <div xmlns='http://www.w3.org/1999/xhtml' style='font-family: Arial, sans-serif; font-size: 38px; line-height: 1.15; font-weight: 700; color: white; text-align: center; word-wrap: break-word;'>
      {title}
    </div>
  </foreignObject>
  <line x1='95' y1='525' x2='385' y2='525' stroke='rgba(255,255,255,0.55)' stroke-width='3'/>
  <text x='240' y='610' text-anchor='middle' font-family='Arial, sans-serif' font-size='30' fill='white'>{year}</text>
</svg>""".encode("utf-8")


def save_generated_cover_for_book(book):
    file_bytes = build_svg_cover(book)
    md5_hash = hashlib.md5(file_bytes).hexdigest()
    cover = Cover(filename="pending", mime_type="image/svg+xml", md5_hash=md5_hash, book=book)
    db.session.add(cover)
    db.session.flush()
    cover.filename = f"{cover.id}.svg"
    return (cover.filename, file_bytes)


def save_cover_for_book(book, uploaded_file):
    if not uploaded_file or not uploaded_file.filename:
        return save_generated_cover_for_book(book)
    if not allowed_cover_file(uploaded_file.filename):
        raise ValueError("Допустимые форматы обложки: png, jpg, jpeg, gif, webp, svg")

    original_filename = secure_filename(uploaded_file.filename)
    file_bytes = uploaded_file.read()
    md5_hash = hashlib.md5(file_bytes).hexdigest()
    extension = os.path.splitext(original_filename)[1].lower() or ".bin"
    mime_type = uploaded_file.mimetype or "application/octet-stream"

    existing_cover = Cover.query.filter_by(md5_hash=md5_hash).first()
    if existing_cover and os.path.exists(os.path.join(app.config["UPLOAD_FOLDER"], existing_cover.filename)):
        cover = Cover(
            filename=existing_cover.filename,
            mime_type=existing_cover.mime_type,
            md5_hash=md5_hash,
            book=book,
        )
        db.session.add(cover)
        return None

    cover = Cover(filename="pending", mime_type=mime_type, md5_hash=md5_hash, book=book)
    db.session.add(cover)
    db.session.flush()
    cover.filename = f"{cover.id}{extension}"
    return (cover.filename, file_bytes)


def validate_book_form(is_edit=False):
    title = (request.form.get("title") or "").strip()
    description = clean_markdown_source(request.form.get("description") or "")
    publisher = (request.form.get("publisher") or "").strip()
    author = (request.form.get("author") or "").strip()
    genre_ids = request.form.getlist("genres")

    if not title:
        raise ValueError("Поле «Название» обязательно")
    if not description:
        raise ValueError("Поле «Краткое описание» обязательно")
    if not publisher:
        raise ValueError("Поле «Издательство» обязательно")
    if not author:
        raise ValueError("Поле «Автор» обязательно")
    if not genre_ids:
        raise ValueError("Необходимо выбрать хотя бы один жанр")

    year = parse_positive_int(request.form.get("year"), "Год", min_value=1, max_value=9999)
    pages = parse_positive_int(request.form.get("pages"), "Объём")

    genres = Genre.query.filter(Genre.id.in_([int(gid) for gid in genre_ids])).all()
    if len(genres) != len(genre_ids):
        raise ValueError("Выбран некорректный жанр")

    return {
        "title": title,
        "description": description,
        "year": year,
        "publisher": publisher,
        "author": author,
        "pages": pages,
        "genres": genres,
        "selected_genres": [genre.id for genre in genres],
    }


def render_book_form(book=None, selected_genres=None):
    genres = Genre.query.order_by(Genre.name).all()
    return render_template(
        "book_form.html",
        book=book,
        genres=genres,
        selected_genres=selected_genres or ([] if not book else [genre.id for genre in book.genres]),
        form_data=request.form,
        is_edit=book is not None,
    )


def parse_date_filter(value, end_of_day=False):
    if not value:
        return None
    parsed_date = datetime.strptime(value, "%Y-%m-%d").date()
    if end_of_day:
        parsed_date = parsed_date + timedelta(days=1)
    msk_boundary = datetime.combine(parsed_date, time.min, tzinfo=MOSCOW_TZ)
    return msk_boundary.astimezone(timezone.utc).replace(tzinfo=None)


def build_view_stats_query(date_from=None, date_to=None):
    query = (
        db.session.query(Book.title, func.count(BookVisit.id).label("views_count"))
        .join(BookVisit, Book.id == BookVisit.book_id)
        .filter(BookVisit.user_id.isnot(None))
    )
    if date_from:
        query = query.filter(BookVisit.created_at >= date_from)
    if date_to:
        query = query.filter(BookVisit.created_at < date_to)
    return query.group_by(Book.id).order_by(func.count(BookVisit.id).desc(), Book.title.asc())


@app.route("/")
def index():
    per_page = 10
    page = request.args.get("page", 1, type=int)
    page = max(page, 1)

    total_books = Book.query.count()
    pagination = make_pagination(page, total_books, per_page)
    page = pagination["page"]

    books = (
        Book.query.order_by(Book.year.desc(), Book.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    review_stats = get_book_review_stats([book.id for book in books])

    return render_template(
        "index.html",
        books=books,
        review_stats=review_stats,
        pagination=pagination,
        popular_books=get_popular_books(),
        recent_books=get_recent_books(),
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        login_value = (request.form.get("login") or "").strip()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))
        user = User.query.filter_by(login=login_value).first()

        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=remember)
            flash("Вы успешно вошли в систему", "success")
            next_url = request.args.get("next")
            if next_url and next_url.startswith("/"):
                return redirect(next_url)
            return redirect(url_for("index"))

        flash("Невозможно аутентифицироваться с указанными логином и паролем", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    if current_user.is_authenticated:
        logout_user()
        flash("Вы вышли из системы", "info")
    next_url = request.referrer or url_for("index")
    if "/logout" in next_url:
        next_url = url_for("index")
    return redirect(next_url)


@app.route("/covers/<path:filename>")
def cover_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/books/<int:book_id>")
def view_book(book_id):
    book = Book.query.get_or_404(book_id)
    record_book_visit(book.id)

    user_review = None
    can_write_review = False
    if current_user.is_authenticated:
        user_review = Review.query.filter_by(book_id=book.id, user_id=current_user.id).first()
        can_write_review = user_review is None

    reviews = Review.query.filter_by(book_id=book.id).order_by(Review.created_at.desc()).all()
    avg_rating = db.session.query(func.avg(Review.rating)).filter_by(book_id=book.id).scalar()

    return render_template(
        "book_view.html",
        book=book,
        reviews=reviews,
        user_review=user_review,
        can_write_review=can_write_review,
        avg_rating=round(avg_rating, 1) if avg_rating is not None else None,
    )


@app.route("/books/add", methods=["GET", "POST"])
@roles_required("administrator")
def add_book():
    if request.method == "POST":
        pending_file_to_save = None
        try:
            data = validate_book_form(is_edit=False)
            book = Book(
                title=data["title"],
                description=data["description"],
                year=data["year"],
                publisher=data["publisher"],
                author=data["author"],
                pages=data["pages"],
                genres=data["genres"],
            )
            db.session.add(book)
            db.session.flush()
            pending_file_to_save = save_cover_for_book(book, request.files.get("cover"))
            db.session.commit()

            if pending_file_to_save:
                filename, file_bytes = pending_file_to_save
                with open(os.path.join(app.config["UPLOAD_FOLDER"], filename), "wb") as file:
                    file.write(file_bytes)

            flash("Книга успешно добавлена", "success")
            return redirect(url_for("view_book", book_id=book.id))
        except Exception:
            db.session.rollback()
            flash("При сохранении данных возникла ошибка. Проверьте корректность введённых данных.", "danger")
            selected = []
            try:
                selected = [int(value) for value in request.form.getlist("genres")]
            except ValueError:
                selected = []
            return render_book_form(selected_genres=selected)

    return render_book_form()


@app.route("/books/<int:book_id>/edit", methods=["GET", "POST"])
@roles_required("administrator", "moderator")
def edit_book(book_id):
    book = Book.query.get_or_404(book_id)

    if request.method == "POST":
        try:
            data = validate_book_form(is_edit=True)
            book.title = data["title"]
            book.description = data["description"]
            book.year = data["year"]
            book.publisher = data["publisher"]
            book.author = data["author"]
            book.pages = data["pages"]
            book.genres = data["genres"]
            db.session.commit()
            flash("Данные книги успешно обновлены", "success")
            return redirect(url_for("view_book", book_id=book.id))
        except Exception:
            db.session.rollback()
            flash("При сохранении данных возникла ошибка. Проверьте корректность введённых данных.", "danger")
            selected = []
            try:
                selected = [int(value) for value in request.form.getlist("genres")]
            except ValueError:
                selected = [genre.id for genre in book.genres]
            return render_book_form(book=book, selected_genres=selected)

    return render_book_form(book=book)


@app.route("/books/<int:book_id>/delete", methods=["POST"])
@roles_required("administrator")
def delete_book(book_id):
    book = Book.query.get_or_404(book_id)
    title = book.title
    cover_filename = book.cover.filename if book.cover else None

    try:
        db.session.delete(book)
        db.session.commit()

        if cover_filename:
            used_elsewhere = Cover.query.filter_by(filename=cover_filename).count()
            if used_elsewhere == 0:
                cover_path = os.path.join(app.config["UPLOAD_FOLDER"], cover_filename)
                if os.path.exists(cover_path):
                    os.remove(cover_path)

        flash(f"Книга «{title}» успешно удалена", "success")
    except Exception:
        db.session.rollback()
        flash("При удалении книги возникла ошибка", "danger")

    return redirect(url_for("index"))


@app.route("/books/<int:book_id>/reviews/add", methods=["GET", "POST"])
@roles_required("administrator", "moderator", "user")
def add_review(book_id):
    book = Book.query.get_or_404(book_id)
    existing_review = Review.query.filter_by(book_id=book.id, user_id=current_user.id).first()
    if existing_review:
        flash("Вы уже оставляли рецензию на эту книгу", "info")
        return redirect(url_for("view_book", book_id=book.id))

    rating_options = [
        (5, "отлично"),
        (4, "хорошо"),
        (3, "удовлетворительно"),
        (2, "неудовлетворительно"),
        (1, "плохо"),
        (0, "ужасно"),
    ]

    if request.method == "POST":
        try:
            rating = int(request.form.get("rating", 5))
            if rating not in [option[0] for option in rating_options]:
                raise ValueError("Некорректная оценка")
            text = clean_markdown_source(request.form.get("text") or "")
            if not text:
                raise ValueError("Текст рецензии обязателен")

            review = Review(book=book, user=current_user, rating=rating, text=text)
            db.session.add(review)
            db.session.commit()
            flash("Рецензия успешно добавлена", "success")
            return redirect(url_for("view_book", book_id=book.id))
        except Exception:
            db.session.rollback()
            flash("При сохранении рецензии возникла ошибка. Проверьте корректность введённых данных.", "danger")

    return render_template("review_form.html", book=book, rating_options=rating_options)


@app.route("/stats")
@roles_required("administrator")
def stats():
    per_page = 10
    tab = request.args.get("tab", "log")
    log_page = max(request.args.get("log_page", 1, type=int), 1)
    stats_page = max(request.args.get("stats_page", 1, type=int), 1)
    date_from_raw = request.args.get("date_from", "")
    date_to_raw = request.args.get("date_to", "")

    date_from = None
    date_to = None
    try:
        date_from = parse_date_filter(date_from_raw)
        date_to = parse_date_filter(date_to_raw, end_of_day=True)
    except ValueError:
        flash("Проверьте корректность дат в фильтре", "warning")

    log_query = BookVisit.query.join(Book).outerjoin(User).order_by(BookVisit.created_at.desc())
    log_total = log_query.count()
    log_pagination = make_pagination(log_page, log_total, per_page)
    log_page = log_pagination["page"]
    log_records = log_query.offset((log_page - 1) * per_page).limit(per_page).all()

    view_stats_query = build_view_stats_query(date_from, date_to)
    view_stats_total = view_stats_query.count()
    view_stats_pagination = make_pagination(stats_page, view_stats_total, per_page)
    stats_page = view_stats_pagination["page"]
    view_stats_rows = view_stats_query.offset((stats_page - 1) * per_page).limit(per_page).all()

    return render_template(
        "stats.html",
        tab=tab,
        log_records=log_records,
        log_pagination=log_pagination,
        log_start=(log_page - 1) * per_page,
        view_stats_rows=view_stats_rows,
        view_stats_pagination=view_stats_pagination,
        stats_start=(stats_page - 1) * per_page,
        date_from=date_from_raw,
        date_to=date_to_raw,
    )


@app.route("/stats/log/export")
@roles_required("administrator")
def export_log_csv():
    rows = BookVisit.query.join(Book).outerjoin(User).order_by(BookVisit.created_at.desc()).all()

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["№", "ФИО пользователя", "Название книги", "Дата и время просмотра"])
    for index, visit in enumerate(rows, start=1):
        writer.writerow(
            [
                index,
                visit.user.full_name if visit.user else "Неаутентифицированный пользователь",
                visit.book.title,
                datetime_filter(visit.created_at),
            ]
        )

    filename = f"user_actions_{moscow_now_date().isoformat()}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/stats/books/export")
@roles_required("administrator")
def export_book_stats_csv():
    date_from_raw = request.args.get("date_from", "")
    date_to_raw = request.args.get("date_to", "")
    date_from = None
    date_to = None
    try:
        date_from = parse_date_filter(date_from_raw)
        date_to = parse_date_filter(date_to_raw, end_of_day=True)
    except ValueError:
        pass

    rows = build_view_stats_query(date_from, date_to).all()

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["№", "Название книги", "Количество просмотров"])
    for index, row in enumerate(rows, start=1):
        writer.writerow([index, row.title, row.views_count])

    filename = f"book_view_stats_{moscow_now_date().isoformat()}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def create_seed_cover(book, color_number):
    file_bytes = build_svg_cover(book, color_number)
    md5_hash = hashlib.md5(file_bytes).hexdigest()
    filename = f"seed_{book.id}.svg"
    path = os.path.join(COVERS_DIR, filename)
    if not os.path.exists(path):
        with open(path, "wb") as file:
            file.write(file_bytes)
    cover = Cover(filename=filename, mime_type="image/svg+xml", md5_hash=md5_hash, book=book)
    db.session.add(cover)


def seed_data():
    if Role.query.count() > 0:
        return

    admin_role = Role(name="administrator", description="Администратор")
    moderator_role = Role(name="moderator", description="Модератор")
    user_role = Role(name="user", description="Пользователь")
    db.session.add_all([admin_role, moderator_role, user_role])

    genres = [
        Genre(name="Классика"),
        Genre(name="Роман"),
        Genre(name="История"),
        Genre(name="Драма"),
        Genre(name="Сатира"),
        Genre(name="Детектив"),
        Genre(name="Фантастика"),
        Genre(name="Антиутопия"),
        Genre(name="Фэнтези"),
        Genre(name="Приключения"),
    ]
    db.session.add_all(genres)
    db.session.flush()

    users = [
        User(
            login="admin",
            password_hash=generate_password_hash("admin"),
            last_name="Иванов",
            first_name="Админ",
            middle_name="Петрович",
            role=admin_role,
        ),
        User(
            login="moderator",
            password_hash=generate_password_hash("moderator"),
            last_name="Смирнова",
            first_name="Мария",
            middle_name="Игоревна",
            role=moderator_role,
        ),
        User(
            login="user",
            password_hash=generate_password_hash("user"),
            last_name="Петров",
            first_name="Кирилл",
            middle_name="Андреевич",
            role=user_role,
        ),
    ]
    db.session.add_all(users)
    db.session.flush()

    genre_by_name = {genre.name: genre for genre in genres}
    book_rows = [
        (
            "Гарри Поттер и философский камень",
            "Первая книга о юном волшебнике, который узнаёт о мире магии и поступает в Хогвартс.",
            1997,
            "Bloomsbury",
            "Дж. К. Роулинг",
            432,
            ["Фэнтези", "Приключения"],
        ),
        (
            "Мастер и Маргарита",
            "Роман Михаила Булгакова, соединяющий сатиру, философскую прозу и мистическую линию Москвы XX века.",
            1967,
            "YMCA-Press",
            "Михаил Булгаков",
            480,
            ["Классика", "Роман", "Сатира"],
        ),
        (
            "451 градус по Фаренгейту",
            "Антиутопия о мире, где книги запрещены, а пожарные занимаются их уничтожением.",
            1953,
            "Ballantine Books",
            "Рэй Брэдбери",
            256,
            ["Фантастика", "Антиутопия"],
        ),
        (
            "1984",
            "Роман-антиутопия о тоталитарном обществе, контроле информации и личной свободе.",
            1949,
            "Secker & Warburg",
            "Джордж Оруэлл",
            328,
            ["Антиутопия", "Роман"],
        ),
        (
            "Хоббит, или Туда и обратно",
            "Приключенческая повесть о путешествии Бильбо Бэггинса, гномов и волшебника Гэндальфа.",
            1937,
            "George Allen & Unwin",
            "Дж. Р. Р. Толкин",
            320,
            ["Фэнтези", "Приключения"],
        ),
        (
            "Двенадцать стульев",
            "Сатирический роман о поиске драгоценностей, спрятанных в одном из двенадцати стульев.",
            1928,
            "Земля и фабрика",
            "Илья Ильф, Евгений Петров",
            384,
            ["Сатира", "Приключения", "Роман"],
        ),
        (
            "Шерлок Холмс: Этюд в багровых тонах",
            "Первое произведение о Шерлоке Холмсе и докторе Ватсоне, с которого начинается знаменитый детективный цикл.",
            1887,
            "Ward Lock & Co",
            "Артур Конан Дойл",
            176,
            ["Детектив", "Классика"],
        ),
        (
            "Война и мир",
            "Эпический роман о судьбах нескольких семей на фоне событий Отечественной войны 1812 года.",
            1869,
            "Русский вестник",
            "Лев Толстой",
            1225,
            ["Классика", "Роман", "История"],
        ),
        (
            "Преступление и наказание",
            "Психологический роман о преступлении Родиона Раскольникова и его нравственном испытании.",
            1866,
            "Русский вестник",
            "Фёдор Достоевский",
            672,
            ["Классика", "Роман", "Драма"],
        ),
        (
            "Отцы и дети",
            "Роман о конфликте поколений, нигилизме и взглядах русской интеллигенции XIX века.",
            1862,
            "Русский вестник",
            "Иван Тургенев",
            288,
            ["Классика", "Роман", "Драма"],
        ),
        (
            "Герой нашего времени",
            "Роман о Печорине, сложном и противоречивом герое русской литературы.",
            1840,
            "Типография Ильи Глазунова",
            "Михаил Лермонтов",
            224,
            ["Классика", "Роман"],
        ),
        (
            "Евгений Онегин",
            "Роман в стихах о любви, выборе и светском обществе первой половины XIX века.",
            1833,
            "Типография А. Смирдина",
            "Александр Пушкин",
            224,
            ["Классика", "Роман"],
        ),
    ]

    books = []
    for index, (title, description, year, publisher, author, pages, genre_names) in enumerate(book_rows, start=1):
        book = Book(
            title=title,
            description=description,
            year=year,
            publisher=publisher,
            author=author,
            pages=pages,
            genres=[genre_by_name[name] for name in genre_names],
        )
        db.session.add(book)
        db.session.flush()
        create_seed_cover(book, index)
        books.append(book)

    db.session.flush()

    reviews = [
        Review(book=books[0], user=users[2], rating=5, text="Интересная книга, легко читается и хорошо подходит для знакомства с жанром фэнтези"),
        Review(book=books[1], user=users[2], rating=5, text="Сильный роман: много сатиры, мистики и запоминающихся сцен"),
        Review(book=books[2], user=users[1], rating=4, text="Актуальная антиутопия о ценности книг и свободного мышления."),
        Review(book=books[8], user=users[2], rating=5, text="Глубокий психологический роман, который заставляет задуматься о выборе и ответственности!"),
    ]
    db.session.add_all(reviews)

    now = utc_now()
    demo_visits = []
    for offset, book in enumerate(books[:8]):
        for repeat in range(1, (8 - offset) + 1):
            demo_visits.append(
                BookVisit(
                    book=book,
                    user=users[0] if repeat % 2 else users[2],
                    visitor_id=None,
                    created_at=now - timedelta(days=offset * 4 + repeat, minutes=repeat * 3),
                )
            )
    demo_visits.extend(
        [
            BookVisit(book=books[0], user=None, visitor_id="demo-guest", created_at=now - timedelta(hours=2)),
            BookVisit(book=books[3], user=None, visitor_id="demo-guest", created_at=now - timedelta(hours=3)),
        ]
    )
    db.session.add_all(demo_visits)
    db.session.commit()


def init_db():
    db.create_all()
    seed_data()


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
