from setuptools import setup

setup(
    name='aws_ecs_service_discovery',
    version='0.1',
    py_modules=['services'],
    install_requires=[
        'boto',
    ],
    entry_points='''
        [console_scripts]
        awsesd=services:cli
    ''',
)
