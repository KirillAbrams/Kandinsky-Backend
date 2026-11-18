# Описывает данные входа, регистрации, восстановления и удаления аккаунта. Отдельные формы требуют
# ровно те сведения, которые нужны выбранной операции.

from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class VerifiedRegistration(Credentials):
    code: str = Field(pattern=r"^[0-9]{8}$")


class AccountDeletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr = Field(min_length=12, max_length=128)


class AccountErasureBatch(BaseModel):
    owners: list[UUID] = Field(min_length=1, max_length=100)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(PasswordResetRequest):
    code: str = Field(pattern=r"^[0-9]{8}$")
    password: str = Field(min_length=12, max_length=128)
