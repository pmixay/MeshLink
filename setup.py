from setuptools import setup, find_packages

setup(
    name="meshlink",
    version="1.0.0",
    description="Decentralized P2P Communication — No Internet, No Servers",
    packages=find_packages(),
    include_package_data=True,
    package_data={"meshlink": ["templates/*", "static/**/*"]},
    python_requires=">=3.9",
    install_requires=[
        "flask>=3.0",
        "flask-socketio>=5.3",
        "cryptography>=41.0",
    ],
    entry_points={
        "console_scripts": [
            "meshlink=meshlink.main:main",
        ],
    },
)
