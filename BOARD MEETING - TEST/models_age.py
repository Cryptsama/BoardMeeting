from sqlalchemy import Column, Integer, String, Date, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from db_age import Base

class User(Base):
    __tablename__ = "users_age"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(320), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    dob = Column(Date, nullable=False)

    is_age_verified = Column(Boolean, default=False, nullable=False)
    age_verified_at = Column(DateTime, nullable=True)

    requests = relationship("AgeVerificationRequest", back_populates="user")

class AgeVerificationRequest(Base):
    __tablename__ = "age_verification_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users_age.id"), nullable=False)

    status = Column(String(20), default="pending", nullable=False)  # pending/approved/denied
    notes = Column(Text, nullable=True)

    stored_path = Column(String(500), nullable=False)
    original_filename = Column(String(255), nullable=False)
    content_type = Column(String(100), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    decided_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="requests")
