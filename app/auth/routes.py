from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import StudentAccount

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("sentences.list_sentences"))

    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        student = StudentAccount.query.filter_by(username=username).first()
        if not student or not student.check_password(password):
            error = "Nieprawidłowy login lub hasło."
        else:
            login_user(student)
            flash("Zalogowano pomyślnie.", "success")
            next_url = request.args.get("next") or url_for("sentences.list_sentences")
            return redirect(next_url)
        if error:
            flash(error, "error")

    return render_template("auth/login.html", error=error)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("sentences.list_sentences"))

    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if not username or not password:
            error = "Login i hasło są wymagane."
        elif len(password) < 8:
            error = "Hasło musi mieć co najmniej 8 znaków."
        elif password != confirm:
            error = "Hasła muszą być identyczne."
        elif StudentAccount.query.filter_by(username=username).first():
            error = "Taki login już istnieje."
        else:
            student = StudentAccount(username=username)
            student.set_password(password)
            db.session.add(student)
            db.session.commit()
            flash("Konto zostało utworzone. Możesz się zalogować.", "success")
            return redirect(url_for("auth.login"))
        if error:
            flash(error, "error")

    return render_template("auth/register.html", error=error)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Wylogowano.", "success")
    return redirect(url_for("auth.login"))
