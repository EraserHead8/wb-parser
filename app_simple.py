from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import requests
import json
import time
import os
from datetime import datetime
import re
import csv
from io import BytesIO
import zipfile
import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
from reportlab.lib.units import inch
import openai
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

app = Flask(__name__)
CORS(app)

# OpenAI API настройки
openai.api_key = os.getenv('OPENAI_API_KEY')
if not openai.api_key:
    raise ValueError("OPENAI_API_KEY environment variable is not set")

# Папка для сохранения файлов
UPLOAD_FOLDER = 'parsed_files'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

class WildberriesParser:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        }
        
    def get_seller_id(self, seller_url):
        """Извлечение ID продавца из URL"""
        # Примеры URL:
        # https://www.wildberries.ru/seller/12345
        # https://www.wildberries.ru/brands/12345
        # https://www.wildberries.ru/brands/brand-name/all
        
        # Пробуем найти числовой ID
        match = re.search(r'/seller/(\d+)', seller_url)
        if match:
            return match.group(1)
        
        match = re.search(r'/brands/(\d+)', seller_url)
        if match:
            return match.group(1)
        
        # Если это текстовый бренд, нужно получить ID через поиск
        match = re.search(r'/brands/([^/]+)', seller_url)
        if match:
            brand_name = match.group(1)
            return self.get_brand_id(brand_name)
        
        raise ValueError("Не удалось извлечь ID продавца из URL")
    
    def get_brand_id(self, brand_name):
        """Получение ID бренда по имени"""
        search_url = f"https://search.wb.ru/exactmatch/ru/common/v4/search?query={brand_name}"
        
        try:
            response = requests.get(search_url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            
            if data.get('data') and data['data'].get('products') and len(data['data']['products']) > 0:
                # Берем supplierId первого товара
                supplier_id = data['data']['products'][0].get('supplierId')
                if supplier_id:
                    return str(supplier_id)
        except Exception as e:
            print(f"Ошибка при получении ID бренда: {e}")
        
        raise ValueError(f"Не удалось найти бренд: {brand_name}")
    
    def parse_seller_products(self, seller_url):
        """Парсинг всех товаров продавца"""
        print(f"\n{'='*50}")
        print(f"НАЧАЛО ПАРСИНГА ПРОДАВЦА")
        print(f"URL: {seller_url}")
        print(f"{'='*50}\n")
        
        try:
            seller_id = self.get_seller_id(seller_url)
            print(f"✓ ID продавца успешно извлечен: {seller_id}")
        except Exception as e:
            error_msg = f"✗ ОШИБКА извлечения ID продавца: {e}"
            print(error_msg)
            raise ValueError(error_msg)
            
        products = []
        page = 1
        total_requests = 0
        consecutive_errors = 0
        max_consecutive_errors = 3
        
        # Статистика для отладки
        stats = {
            'total_pages': 0,
            'total_requests': 0,
            'errors': [],
            'empty_pages': 0,
            'timeout_errors': 0,
            'rate_limit_errors': 0
        }
        
        while True:
            try:
                # Формируем URL
                api_url = f"https://catalog.wb.ru/sellers/catalog?appType=1&curr=rub&dest=-1257786&page={page}&sort=popular&supplier={seller_id}"
                
                print(f"\n--- Страница {page} ---")
                print(f"Текущее количество товаров: {len(products)}")
                
                # Большая пауза каждые 1000 товаров
                if len(products) > 0 and len(products) % 1000 == 0:
                    pause_time = 15  # Увеличиваем паузу до 15 секунд
                    print(f"⏸️  БОЛЬШАЯ ПАУЗА {pause_time} сек после {len(products)} товаров...")
                    time.sleep(pause_time)
                
                # Средняя пауза каждые 20 страниц
                if page > 1 and page % 20 == 0:
                    pause_time = 8  # Увеличиваем паузу до 8 секунд
                    print(f"⏸️  Пауза {pause_time} сек после {page} страниц...")
                    time.sleep(pause_time)
                
                # Делаем запрос с увеличенным таймаутом
                print(f"→ Отправка запроса...")
                start_time = time.time()
                
                response = requests.get(
                    api_url, 
                    headers=self.headers, 
                    timeout=30  # Увеличенный таймаут
                )
                
                request_time = time.time() - start_time
                print(f"← Ответ получен за {request_time:.2f} сек, статус: {response.status_code}")
                
                total_requests += 1
                stats['total_requests'] = total_requests
                
                # Проверка статуса
                if response.status_code == 429:
                    stats['rate_limit_errors'] += 1
                    wait_time = 60
                    print(f"⚠️  Ошибка 429: Слишком много запросов. Ожидание {wait_time} сек...")
                    time.sleep(wait_time)
                    continue
                    
                elif response.status_code != 200:
                    error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                    print(f"✗ Ошибка ответа: {error_msg}")
                    stats['errors'].append({'page': page, 'error': error_msg})
                    consecutive_errors += 1
                    
                    if consecutive_errors >= max_consecutive_errors:
                        print(f"✗ Слишком много ошибок подряд ({consecutive_errors}). Остановка.")
                        break
                    
                    time.sleep(5)
                    continue
                
                # Парсинг JSON
                try:
                    data = response.json()
                except json.JSONDecodeError as e:
                    error_msg = f"JSON decode error: {e}"
                    print(f"✗ Ошибка парсинга JSON: {error_msg}")
                    print(f"   Ответ сервера: {response.text[:500]}...")
                    stats['errors'].append({'page': page, 'error': error_msg})
                    consecutive_errors += 1
                    
                    if consecutive_errors >= max_consecutive_errors:
                        break
                    continue
                
                # Проверка структуры данных
                if not isinstance(data, dict):
                    print(f"✗ Неверный формат данных: {type(data)}")
                    break
                
                if 'data' not in data:
                    print(f"✗ Отсутствует поле 'data' в ответе")
                    print(f"   Структура: {list(data.keys()) if isinstance(data, dict) else 'не словарь'}")
                    break
                
                if not isinstance(data.get('data'), dict):
                    print(f"✗ Поле 'data' не является словарем: {type(data.get('data'))}")
                    break
                
                if 'products' not in data['data']:
                    print(f"✗ Отсутствует поле 'products' в data")
                    stats['empty_pages'] += 1
                    break
                
                products_on_page = data['data']['products']
                
                if not isinstance(products_on_page, list):
                    print(f"✗ products не является списком: {type(products_on_page)}")
                    break
                
                if not products_on_page:
                    print(f"✓ Страница {page} пустая - достигнут конец каталога")
                    stats['empty_pages'] += 1
                    break
                
                print(f"✓ Найдено товаров на странице: {len(products_on_page)}")
                consecutive_errors = 0  # Сброс счетчика ошибок
                
                # Обработка товаров
                page_errors = 0
                for i, product in enumerate(products_on_page):
                    try:
                        if not isinstance(product, dict):
                            page_errors += 1
                            continue
                            
                        product_info = self.extract_product_info(product)
                        products.append(product_info)
                        
                        # Микропауза каждые 100 товаров
                        if len(products) % 100 == 0:
                            time.sleep(0.5)  # Увеличиваем микропаузу до 0.5 секунд
                        
                        # Прогресс каждые 500 товаров
                        if len(products) % 500 == 0:
                            print(f"   ✓ Обработано товаров: {len(products)}")
                            
                    except Exception as e:
                        page_errors += 1
                        if page_errors <= 3:  # Логируем только первые 3 ошибки
                            print(f"   ⚠️  Ошибка товара {i}: {e}")
                
                if page_errors > 0:
                    print(f"   ⚠️  Ошибок на странице: {page_errors}")
                
                # Проверка на последнюю страницу
                if len(products_on_page) < 100:
                    print(f"✓ Последняя страница (товаров < 100)")
                    break
                
                # Ограничение для безопасности
                if len(products) >= 100000:  # Увеличиваем лимит до 100,000
                    print(f"✓ Достигнут лимит в 100,000 товаров")
                    break
                
                # Следующая страница
                page += 1
                stats['total_pages'] = page
                
                # Базовая пауза между страницами
                time.sleep(3)  # Увеличиваем базовую паузу до 3 секунд
                
            except requests.exceptions.Timeout:
                stats['timeout_errors'] += 1
                consecutive_errors += 1
                print(f"⚠️  Таймаут на странице {page}. Попытка {consecutive_errors}/{max_consecutive_errors}")
                
                if consecutive_errors >= max_consecutive_errors:
                    print(f"✗ Слишком много таймаутов. Остановка.")
                    break
                    
                time.sleep(10)  # Увеличиваем паузу при таймауте до 10 секунд
                continue
                
            except requests.exceptions.ConnectionError as e:
                error_msg = f"Connection error: {str(e)}"
                print(f"⚠️  Ошибка соединения: {error_msg}")
                stats['errors'].append({'page': page, 'error': error_msg})
                consecutive_errors += 1
                
                if consecutive_errors >= max_consecutive_errors:
                    break
                    
                time.sleep(15)  # Увеличиваем паузу при ошибке соединения до 15 секунд
                continue
                
            except KeyboardInterrupt:
                print(f"\n⚠️  Прерывание пользователем")
                break
                
            except Exception as e:
                error_msg = f"{type(e).__name__}: {str(e)}"
                print(f"✗ НЕОЖИДАННАЯ ОШИБКА на странице {page}: {error_msg}")
                import traceback
                traceback.print_exc()
                stats['errors'].append({'page': page, 'error': error_msg})
                consecutive_errors += 1
                
                if consecutive_errors >= max_consecutive_errors:
                    break
                    
                time.sleep(10)  # Увеличиваем паузу при неожиданной ошибке до 10 секунд
        
        # Итоговая статистика
        print(f"\n{'='*50}")
        print(f"ИТОГИ ПАРСИНГА:")
        print(f"  • Всего товаров: {len(products)}")
        print(f"  • Обработано страниц: {stats['total_pages']}")
        print(f"  • Всего запросов: {stats['total_requests']}")
        print(f"  • Ошибок таймаута: {stats['timeout_errors']}")
        print(f"  • Ошибок rate limit: {stats['rate_limit_errors']}")
        print(f"  • Пустых страниц: {stats['empty_pages']}")
        print(f"  • Всего ошибок: {len(stats['errors'])}")
        
        if stats['errors']:
            print(f"\nПоследние ошибки:")
            for err in stats['errors'][-5:]:
                print(f"  - Страница {err['page']}: {err['error'][:100]}...")
        
        print(f"{'='*50}\n")
        
        return products
    
    def get_product_stocks(self, product_id):
        """Получение точных остатков товара через отдельный API"""
        try:
            # API для получения остатков
            stocks_url = f"https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&nm={product_id}"
            
            response = requests.get(stocks_url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            
            total_stock = 0
            if data.get('data') and data['data'].get('products'):
                product = data['data']['products'][0]
                if 'sizes' in product:
                    for size in product['sizes']:
                        if 'stocks' in size:
                            for stock in size['stocks']:
                                qty = stock.get('qty', 0)
                                if qty and qty > 0:
                                    total_stock += qty
            
            return total_stock
        except:
            return 0
    
    def extract_product_info(self, product_data):
        """Извлечение информации о товаре"""
        product_id = str(product_data.get('id', ''))
        name = product_data.get('name', '')
        brand = product_data.get('brand', '')
        
        price = product_data.get('priceU', 0) / 100 if product_data.get('priceU') else 0
        sale_price = product_data.get('salePriceU', 0) / 100 if product_data.get('salePriceU') else 0
        
        rating = product_data.get('rating', 0)
        feedbacks = product_data.get('feedbacks', 0)
        
        product_url = f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx"
        
        colors = product_data.get('colors', [])
        sizes = product_data.get('sizes', [])
        
        # Определяем статус товара и остатки
        total_stock = 0
        is_available = False
        
        # Метод 1: Проверка sizes и stocks
        if sizes:
            for size in sizes:
                if isinstance(size, dict):
                    # Проверяем наличие товара
                    if size.get('available', False):
                        is_available = True
                    
                    # Считаем остатки
                    if 'stocks' in size and isinstance(size['stocks'], list):
                        for stock in size['stocks']:
                            if isinstance(stock, dict):
                                qty = stock.get('qty', 0)
                                if isinstance(qty, (int, float)) and qty > 0:
                                    total_stock += qty
        
        # Метод 2: Проверка общих полей
        if total_stock == 0:
            for field in ['qty', 'quantity', 'volume', 'totalQuantity']:
                if field in product_data:
                    value = product_data.get(field, 0)
                    if isinstance(value, (int, float)) and value > 0:
                        total_stock = value
                        is_available = True
                        break
        
        # Определяем статус товара
        status = "В продаже" if is_available else "Нет в продаже"
        
        # Извлекаем описание товара
        description = product_data.get('description', '')
        
        return {
            'Наименование': name,
            'Ссылка': product_url,
            'Артикул': product_id,
            'Бренд': brand,
            'Оценка': rating,
            'Количество отзывов': feedbacks,
            'Цена со скидкой': sale_price,
            'Цена без скидки': price,
            'Цвета': ', '.join([c.get('name', '') for c in colors]) if colors else '',
            'Размеры': ', '.join([str(s.get('origName', '')) for s in sizes]) if sizes else '',
            'Категория': product_data.get('subjectName', ''),
            'Остаток': total_stock if total_stock > 0 else 0,
            'Статус': status,
            'Описание': description
        }
    
    def search_product_position(self, product_url, keyword):
        """Поиск позиции товара по ключевому слову"""
        print(f"\n=== НАЧАЛО ПОИСКА ===")
        print(f"URL товара: {product_url}")
        print(f"Ключевое слово: {keyword}")
        
        try:
            # Извлекаем ID товара из URL
            match = re.search(r'/catalog/(\d+)/', product_url)
            if not match:
                print(f"ОШИБКА: Не удалось извлечь ID из URL: {product_url}")
                return 0
            
            product_id = match.group(1)
            print(f"ID товара: {product_id}")
            
            position = 0
            page = 1
            
            while page <= 10:  # Ищем в первых 10 страницах
                try:
                    # Формируем URL для поиска
                    search_url = f"https://search.wb.ru/exactmatch/ru/common/v4/search?appType=1&curr=rub&dest=-1257786&page={page}&query={requests.utils.quote(keyword)}&resultset=catalog&sort=popular&spp=30&suppressSpellcheck=false"
                    
                    print(f"\nСтраница {page}: {search_url}")
                    
                    # Делаем запрос
                    response = requests.get(search_url, headers=self.headers, timeout=10)
                    print(f"Статус ответа: {response.status_code}")
                    response.raise_for_status()
                    
                    # Парсим JSON
                    try:
                        data = response.json()
                    except json.JSONDecodeError as e:
                        print(f"ОШИБКА декодирования JSON: {e}")
                        print(f"Ответ сервера: {response.text[:500]}")
                        break
                    
                    # Проверяем структуру данных
                    if not isinstance(data, dict):
                        print(f"ОШИБКА: Неожиданный тип данных: {type(data)}")
                        break
                    
                    if 'data' not in data:
                        print(f"ОШИБКА: Нет поля 'data' в ответе")
                        print(f"Структура ответа: {list(data.keys())}")
                        break
                    
                    if not isinstance(data['data'], dict):
                        print(f"ОШИБКА: data не является словарем: {type(data['data'])}")
                        break
                    
                    if 'products' not in data['data']:
                        print(f"ОШИБКА: Нет поля 'products' в data")
                        print(f"Структура data: {list(data['data'].keys())}")
                        break
                    
                    products = data['data']['products']
                    if not isinstance(products, list):
                        print(f"ОШИБКА: products не является списком: {type(products)}")
                        break
                    
                    print(f"Найдено товаров: {len(products)}")
                    
                    # Проверяем каждый товар
                    for i, product in enumerate(products):
                        position += 1
                        
                        if not isinstance(product, dict):
                            print(f"Товар {i} не является словарем")
                            continue
                        
                        # Получаем ID текущего товара
                        current_id = product.get('id')
                        
                        # Выводим первые несколько товаров для отладки
                        if i < 3:
                            print(f"Товар {position}: ID={current_id}, тип={type(current_id)}")
                        
                        # Безопасное сравнение
                        if current_id is not None:
                            try:
                                # Преобразуем оба значения в строки
                                current_id_str = str(current_id).strip()
                                product_id_str = str(product_id).strip()
                                
                                if current_id_str == product_id_str:
                                    print(f"\n✓ ТОВАР НАЙДЕН на позиции {position}!")
                                    return position
                            except Exception as e:
                                print(f"ОШИБКА при сравнении ID: {e}")
                                print(f"current_id={current_id}, product_id={product_id}")
                    
                    # Проверяем, есть ли еще страницы
                    if len(products) < 100:
                        print(f"Последняя страница (товаров: {len(products)})")
                        break
                    
                    page += 1
                    time.sleep(0.5)
                    
                except requests.exceptions.RequestException as e:
                    print(f"ОШИБКА сети: {e}")
                    break
                except Exception as e:
                    print(f"НЕОЖИДАННАЯ ОШИБКА: {e}")
                    import traceback
                    traceback.print_exc()
                    break
            
            print(f"\n✗ Товар НЕ НАЙДЕН после проверки {position} позиций")
            return 0
            
        except Exception as e:
            print(f"\nКРИТИЧЕСКАЯ ОШИБКА: {e}")
            import traceback
            traceback.print_exc()
            return 0
        finally:
            print("=== КОНЕЦ ПОИСКА ===\n")

    def analyze_ad_rates(self, query, region):
        """Анализ рекламных ставок по запросу"""
        results = []
        page = 1
        position = 0
        
        print(f"\n=== АНАЛИЗ РЕКЛАМНЫХ СТАВОК ===")
        print(f"Запрос: {query}")
        print(f"Регион: {region}")
        
        try:
            # Используем API поиска WB для получения данных о рекламе
            search_url = f"https://search.wb.ru/exactmatch/ru/common/v4/search?appType=1&curr=rub&dest={region}&page={page}&query={requests.utils.quote(query)}&resultset=catalog&sort=popular&spp=30&suppressSpellcheck=false"
            
            print(f"URL запроса: {search_url}")
            
            response = requests.get(search_url, headers=self.headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if data.get('data') and data['data'].get('products'):
                products = data['data']['products']
                print(f"Найдено товаров: {len(products)}")
                
                # Анализируем первые 50 позиций
                for product in products[:50]:
                    position += 1
                    
                    # Извлекаем данные о рекламе
                    product_id = str(product.get('id', ''))
                    log = product.get('log', {})
                    
                    # Определяем, является ли позиция рекламной
                    is_ad = False
                    position_type = 'Поиск'
                    cpm_value = 0
                    
                    # Метод 1: Проверка поля log
                    if isinstance(log, dict):
                        # Проверяем различные поля, указывающие на рекламу
                        if log.get('tp') == 'a':
                            is_ad = True
                            position_type = 'Автореклама'
                        
                        # CPM может быть в разных полях
                        cpm_value = log.get('cpm', 0)
                        if not cpm_value:
                            cpm_value = log.get('cpmReal', 0)
                    
                    # Метод 2: Проверка строкового представления log
                    if not is_ad and log:
                        log_str = str(log).lower()
                        if 'advert' in log_str or 'cpm' in log_str:
                            is_ad = True
                            position_type = 'Автореклама'
                            
                            # Пытаемся извлечь CPM из строки
                            if not cpm_value:
                                import re
                                cpm_match = re.search(r'cpm["\s:]+(\d+)', str(log))
                                if cpm_match:
                                    cpm_value = int(cpm_match.group(1))
                    
                    # Метод 3: Проверка promo поля
                    if product.get('promo'):
                        is_ad = True
                        if not position_type.startswith('Авто'):
                            position_type = 'Реклама'
                    
                    # Метод 4: Проверка позиции (первые позиции часто рекламные)
                    if position <= 4 and not is_ad:
                        # Проверяем дополнительные признаки
                        if product.get('promoTextCard') or product.get('promoTextCat'):
                            is_ad = True
                            position_type = 'Автореклама'
                    
                    # Расчет ставки на основе CPM
                    bid = 0
                    if cpm_value > 0:
                        # Формула расчета ставки (примерная)
                        # Ставка = CPM * коэффициент
                        bid = round(cpm_value * 0.35)
                    elif is_ad:
                        # Если CPM не найден, но это реклама, делаем оценку
                        if position <= 5:
                            bid = 500 - (position - 1) * 50
                            cpm_value = round(bid / 0.35)
                        elif position <= 10:
                            bid = 250 - (position - 5) * 20
                            cpm_value = round(bid / 0.35)
                        else:
                            bid = 150
                            cpm_value = round(bid / 0.35)
                    
                    # Дополнительная информация
                    spp = log.get('spp', '') if isinstance(log, dict) else ''
                    promo_position = log.get('position', position) if isinstance(log, dict) else position
                    
                    result = {
                        'position': position,
                        'type': position_type,
                        'article': product_id,
                        'cpm': cpm_value,
                        'bid': bid,
                        'name': product.get('name', '')[:50] + '...' if len(product.get('name', '')) > 50 else product.get('name', ''),
                        'spp': spp,
                        'promo_position': promo_position if is_ad else ''
                    }
                    
                    results.append(result)
                    
                    # Выводим информацию о рекламных позициях
                    if is_ad:
                        print(f"Позиция {position}: {position_type}, CPM={cpm_value}, Ставка={bid}")
                
                print(f"\nВсего проанализировано позиций: {len(results)}")
                print(f"Рекламных позиций: {sum(1 for r in results if r['type'] != 'Поиск')}")
                
            else:
                print("Товары не найдены")
                
        except Exception as e:
            print(f"Ошибка при анализе ставок: {str(e)}")
            import traceback
            traceback.print_exc()
            
        return results
    
    def analyze_competitors(self, product_url):
        """Анализ конкурентов в категории товара"""
        match = re.search(r'/catalog/(\d+)/', product_url)
        if not match:
            raise ValueError("Не удалось извлечь ID товара из URL")
        
        product_id = match.group(1)
        
        product_info_url = f"https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&nm={product_id}"
        
        try:
            response = requests.get(product_info_url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            
            if not data.get('data') or not data['data'].get('products'):
                raise ValueError("Товар не найден")
            
            product_data = data['data']['products'][0]
            category = product_data.get('subjectName', '')
            your_price = product_data.get('salePriceU', 0) / 100 if product_data.get('salePriceU') else 0
            
            competitors = []
            position_found = False
            your_position = 0
            total_price = 0
            total_rating = 0
            sellers_count = {}
            page = 1
            
            while len(competitors) < 100 and page <= 10:
                search_url = f"https://search.wb.ru/exactmatch/ru/common/v4/search?appType=1&curr=rub&dest=-1257786&page={page}&query={requests.utils.quote(category)}&resultset=catalog&sort=popular&limit=100"
                
                response = requests.get(search_url, headers=self.headers)
                response.raise_for_status()
                search_data = response.json()
                
                if search_data.get('data') and search_data['data'].get('products'):
                    for product in search_data['data']['products']:
                        if len(competitors) >= 100:
                            break
                            
                        current_position = len(competitors) + 1
                        price = product.get('salePriceU', 0) / 100 if product.get('salePriceU') else 0
                        rating = product.get('rating', 0)
                        seller = product.get('brand', 'Неизвестно')
                        
                        competitor = {
                            'position': current_position,
                            'article': str(product.get('id', '')),
                            'name': product.get('name', '')[:60] + '...' if len(product.get('name', '')) > 60 else product.get('name', ''),
                            'price': price,
                            'rating': rating,
                            'feedbacks': product.get('feedbacks', 0),
                            'seller': seller
                        }
                        
                        competitors.append(competitor)
                        
                        if price > 0:
                            total_price += price
                        if rating > 0:
                            total_rating += rating
                        sellers_count[seller] = sellers_count.get(seller, 0) + 1
                        
                        if str(product.get('id', '')) == product_id:
                            position_found = True
                            your_position = current_position
                    
                    if len(search_data['data']['products']) < 100:
                        break
                        
                    page += 1
                    time.sleep(0.3)
                else:
                    break
            
            top_sellers = sorted(sellers_count.items(), key=lambda x: x[1], reverse=True)[:5]
            
            competitors_with_price = [c for c in competitors if c['price'] > 0]
            avg_price = total_price / len(competitors_with_price) if competitors_with_price else 0
            
            competitors_with_rating = [c for c in competitors if c['rating'] > 0]
            avg_rating = total_rating / len(competitors_with_rating) if competitors_with_rating else 0
            
            summary = {
                'position': your_position if position_found else 'Не в топ-100',
                'avg_price': round(avg_price, 2),
                'your_price': your_price,
                'price_status': 'выше' if your_price > avg_price else 'ниже',
                'avg_rating': round(avg_rating, 1),
                'top_sellers': [s[0] for s in top_sellers],
                'category': category,
                'total_competitors': len(competitors)
            }
            
            return {
                'success': True,
                'competitors': competitors[:50],
                'summary': summary,
                'your_product_id': product_id
            }
            
        except Exception as e:
            print(f"Ошибка при анализе конкурентов: {str(e)}")
            raise

    def get_description_playwright(self, product_url):
        """Парсит описание товара через Playwright (эмулирует браузер, кликает по вкладке 'Характеристики и описание')"""
        try:
            print(f"[SEO] Playwright: Получаем описание для {product_url}")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
                page.goto(product_url, timeout=30000)
                page.wait_for_timeout(2000)

                # Кликаем по вкладке/кнопке, если она есть
                try:
                    btn = page.query_selector('button:has-text("Характеристики и описание"), button:has-text("Описание"), div:has-text("Характеристики и описание"), div:has-text("Описание")')
                    if btn:
                        btn.click()
                        print('[SEO] Playwright: Клик по вкладке описания')
                        page.wait_for_timeout(1500)
                except Exception as e:
                    print(f'[SEO] Playwright: Ошибка поиска/клика по вкладке: {e}')

                # Ждем появления pop-up с описанием
                try:
                    page.wait_for_selector('div.popup__content', timeout=7000)
                except Exception as e:
                    print(f'[SEO] Playwright: Не дождался popup: {e}')

                # Собираем текст из всех .option__text--md и .option__text внутри popup
                desc = ""
                try:
                    popup = page.query_selector('div.popup__content')
                    if popup:
                        paragraphs = popup.query_selector_all('.option__text--md, .option__text')
                        texts = [p.inner_text().strip() for p in paragraphs if p.inner_text().strip()]
                        desc = "\n".join(texts)
                except Exception as e:
                    print(f'[SEO] Playwright: Ошибка сбора описания: {e}')

                browser.close()
                if desc:
                    print(f"[SEO] Playwright: Описание найдено, длина {len(desc)}")
                    return desc
                else:
                    print(f"[SEO] Playwright: Описание не найдено")
                    return "Описание отсутствует (Playwright)"
        except Exception as e:
            print(f"[SEO] Playwright: Ошибка: {e}")
            return "Описание отсутствует (ошибка Playwright)"

    def get_description_from_html(self, product_url):
        """Парсит описание товара из HTML, если в API оно отсутствует"""
        try:
            print(f"[SEO] Парсим описание из HTML: {product_url}")
            headers = self.headers.copy()
            headers['Accept'] = 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
            response = requests.get(product_url, headers=headers, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            desc_block = soup.find('div', {'data-link': 'text'})
            if desc_block and desc_block.text.strip():
                return desc_block.text.strip()
            for h4 in soup.find_all(['h4', 'h3']):
                if 'Описание' in h4.text:
                    next_el = h4.find_next_sibling(['div', 'p'])
                    if next_el and next_el.text.strip():
                        return next_el.text.strip()
            desc_alt = soup.find('div', class_='product-about__text')
            if desc_alt and desc_alt.text.strip():
                return desc_alt.text.strip()
            # Если не нашли — пробуем Playwright
            print("[SEO] HTML: Описание не найдено, пробуем Playwright")
            return self.get_description_playwright(product_url)
        except Exception as e:
            print(f"[SEO] Ошибка парсинга описания из HTML: {e}")
            # Если ошибка — пробуем Playwright
            return self.get_description_playwright(product_url)

    def analyze_seo(self, product_url):
        """Анализ SEO товара"""
        print(f"\n=== АНАЛИЗ SEO ===")
        print(f"URL товара: {product_url}")
        try:
            product_url = product_url.strip()
            if product_url.startswith('@'):
                product_url = product_url[1:].strip()
            print(f"URL товара (после удаления @): {product_url}")
            if not product_url:
                print("ОШИБКА: URL товара пустой")
                return jsonify({'error': 'Ссылка на товар не указана'}), 400
            match = re.search(r'/catalog/(\d+)/', product_url)
            if not match:
                print(f"ОШИБКА: Не удалось найти ID товара в URL: {product_url}")
                return jsonify({'error': 'Неверный формат ссылки на товар Wildberries'}), 400
            product_id = match.group(1)
            product_url = f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx"
            print(f"URL преобразован в: {product_url}")
            product_info_url = f"https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&nm={product_id}"
            print(f"Запрашиваем информацию по URL: {product_info_url}")
            response = requests.get(product_info_url, headers=self.headers)
            print(f"Статус ответа: {response.status_code}")
            if response.status_code != 200:
                print(f"ПРОБЛЕМА 2: Ошибка при получении данных товара. Статус: {response.status_code}")
                raise ValueError(f"Ошибка при получении данных товара: {response.status_code}")
            try:
                data = response.json()
            except json.JSONDecodeError as e:
                print(f"ПРОБЛЕМА 3: Ошибка при разборе JSON ответа: {e}")
                raise ValueError("Ошибка при разборе данных товара")
            if not data.get('data') or not data['data'].get('products'):
                print("ПРОБЛЕМА 5: В ответе отсутствуют данные о товаре")
                raise ValueError("Товар не найден")
            product_data = data['data']['products'][0]
            current_description = product_data.get('description', '')
            category = product_data.get('subjectName', '')
            # Если описания нет в API — парсим из HTML
            if not current_description or not current_description.strip():
                print("ПРОБЛЕМА 6: У товара отсутствует описание в API, пробуем HTML")
                current_description = self.get_description_from_html(product_url)
            if not current_description or not current_description.strip():
                current_description = "Описание отсутствует"
            print(f"Категория: {category}")
            print(f"Длина описания: {len(current_description)} символов")
            try:
                keywords = self.extract_keywords(current_description)
                print(f"Найдено ключевых слов: {len(keywords)}")
            except Exception as e:
                print(f"ПРОБЛЕМА 7: Ошибка при извлечении ключевых слов: {e}")
                keywords = []
            try:
                competitors = self.analyze_competitors_seo(category)
                print(f"Проанализировано конкурентов: {len(competitors)}")
            except Exception as e:
                print(f"Ошибка при анализе конкурентов: {e}")
                competitors = []
            try:
                recommendations = self.generate_seo_recommendations(
                    current_description,
                    keywords,
                    competitors
                )
            except Exception as e:
                print(f"Ошибка при генерации рекомендаций: {e}")
                recommendations = []
            try:
                optimized_description = self.generate_optimized_description(
                    current_description,
                    keywords,
                    competitors,
                    recommendations
                )
            except Exception as e:
                print(f"Ошибка при генерации оптимизированного описания: {e}")
                optimized_description = current_description
            return {
                'current_description': current_description,
                'keywords': keywords,
                'competitors': competitors,
                'recommendations': recommendations,
                'optimized_description': optimized_description
            }
        except Exception as e:
            print(f"КРИТИЧЕСКАЯ ОШИБКА при анализе SEO: {str(e)}")
            import traceback
            traceback.print_exc()
            raise
    
    def extract_keywords(self, text):
        """Извлечение ключевых слов из текста с помощью OpenAI"""
        try:
            # Подготавливаем промпт для OpenAI
            prompt = f"""Проанализируй следующий текст и выдели ключевые слова и фразы, которые важны для SEO.
            Для каждого ключевого слова укажи его важность (1-5, где 5 - максимальная важность).
            Текст: {text}
            
            Формат ответа:
            ключевое_слово: важность
            ключевая_фраза: важность
            """

            # Запрос к OpenAI
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Ты - эксперт по SEO и анализу текста."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=500
            )

            # Парсим ответ
            result = response.choices[0].message.content
            keywords = []
            
            for line in result.split('\n'):
                if ':' in line:
                    keyword, importance = line.split(':')
                    keyword = keyword.strip()
                    try:
                        importance = int(importance.strip())
                        keywords.append({
                            'text': keyword,
                            'importance': importance,
                            'important': importance >= 4
                        })
                    except ValueError:
                        continue

            return keywords

        except Exception as e:
            print(f"Ошибка при извлечении ключевых слов: {str(e)}")
            # Возвращаем базовый анализ в случае ошибки
            return self._basic_keyword_extraction(text)

    def _basic_keyword_extraction(self, text):
        """Базовый метод извлечения ключевых слов без использования AI"""
        # Очищаем текст
        text = text.lower()
        text = re.sub(r'[^\w\s]', ' ', text)
        
        # Разбиваем на слова
        words = text.split()
        
        # Удаляем стоп-слова
        stop_words = {'и', 'в', 'во', 'не', 'что', 'к', 'на', 'я', 'с', 'со', 'как', 'а', 'то', 'все', 'она', 'так', 'его', 'но', 'да', 'ты', 'к', 'у', 'же', 'вы', 'за', 'бы', 'по', 'только', 'ее', 'мне', 'было', 'вот', 'от', 'меня', 'еще', 'нет', 'о', 'из', 'ему', 'теперь', 'когда', 'даже', 'ну', 'вдруг', 'ли', 'если', 'уже', 'или', 'ни', 'быть', 'был', 'него', 'до', 'вас', 'нибудь', 'опять', 'уж', 'вам', 'ведь', 'там', 'потом', 'себя', 'ничего', 'ей', 'может', 'они', 'тут', 'где', 'есть', 'надо', 'ней', 'для', 'мы', 'тебя', 'их', 'чем', 'была', 'сам', 'чтоб', 'без', 'будто', 'чего', 'раз', 'тоже', 'себе', 'под', 'будет', 'ж', 'тогда', 'кто', 'этот', 'того', 'потому', 'этого', 'какой', 'совсем', 'ним', 'здесь', 'этом', 'один', 'почти', 'мой', 'тем', 'чтобы', 'нее', 'сейчас', 'были', 'куда', 'зачем', 'всех', 'никогда', 'можно', 'при', 'наконец', 'два', 'об', 'другой', 'хоть', 'после', 'над', 'больше', 'тот', 'через', 'эти', 'нас', 'про', 'всего', 'них', 'какая', 'много', 'разве', 'три', 'эту', 'моя', 'впрочем', 'хорошо', 'свою', 'этой', 'перед', 'иногда', 'лучше', 'чуть', 'том', 'нельзя', 'такой', 'им', 'более', 'всегда', 'конечно', 'всю', 'между'}
        words = [w for w in words if w not in stop_words and len(w) > 2]
        
        # Считаем частоту слов
        word_freq = {}
        for word in words:
            word_freq[word] = word_freq.get(word, 0) + 1
        
        # Сортируем по частоте
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        
        # Формируем результат
        keywords = []
        for word, freq in sorted_words[:20]:  # Берем топ-20 слов
            keywords.append({
                'text': word,
                'importance': min(5, freq),  # Преобразуем частоту в важность
                'important': freq > 2
            })
        
        return keywords

    def analyze_competitors_seo(self, category):
        """Анализ SEO конкурентов в категории"""
        competitors = []
        page = 1
        
        try:
            while len(competitors) < 10 and page <= 3:  # Анализируем топ-10 конкурентов
                search_url = f"https://search.wb.ru/exactmatch/ru/common/v4/search?appType=1&curr=rub&dest=-1257786&page={page}&query={requests.utils.quote(category)}&resultset=catalog&sort=popular&limit=100"
                
                response = requests.get(search_url, headers=self.headers)
                response.raise_for_status()
                data = response.json()
                
                if data.get('data') and data['data'].get('products'):
                    for product in data['data']['products']:
                        if len(competitors) >= 10:
                            break
                            
                        # Получаем полное описание товара
                        product_id = product.get('id')
                        if not product_id:
                            continue
                            
                        product_info_url = f"https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&nm={product_id}"
                        product_response = requests.get(product_info_url, headers=self.headers)
                        product_response.raise_for_status()
                        product_data = product_response.json()
                        
                        if product_data.get('data') and product_data['data'].get('products'):
                            product_info = product_data['data']['products'][0]
                            description = product_info.get('description', '')
                            
                            # Анализируем описание
                            keywords = self.extract_keywords(description)
                            
                            # Определяем особенности описания
                            features = []
                            if len(description) > 1000:
                                features.append('Длинное описание')
                            if len(keywords) > 10:
                                features.append('Много ключевых слов')
                            if any(k['important'] for k in keywords):
                                features.append('Важные ключевые слова')
                            
                            competitor = {
                                'position': len(competitors) + 1,
                                'name': product.get('name', '')[:100] + '...' if len(product.get('name', '')) > 100 else product.get('name', ''),
                                'keywords': [k['text'] for k in keywords[:5]],  # Топ-5 ключевых слов
                                'features': features
                            }
                            
                            competitors.append(competitor)
                
                page += 1
                time.sleep(1)
                
        except Exception as e:
            print(f"Ошибка при анализе конкурентов: {str(e)}")
        
        return competitors
    
    def generate_seo_recommendations(self, description, keywords, competitors):
        """Генерация рекомендаций по SEO с помощью OpenAI"""
        try:
            # Подготавливаем данные о конкурентах
            competitors_info = "\n".join([
                f"Позиция {c['position']}: {c['name']}\n"
                f"Ключевые слова: {', '.join(c['keywords'])}\n"
                f"Особенности: {', '.join(c['features'])}"
                for c in competitors[:3]  # Берем топ-3 конкурента
            ])

            # Подготавливаем промпт
            prompt = f"""Проанализируй описание товара и дай рекомендации по улучшению SEO.
            
            Текущее описание:
            {description}
            
            Ключевые слова:
            {', '.join([k['text'] for k in keywords])}
            
            Информация о конкурентах:
            {competitors_info}
            
            Дай конкретные рекомендации по улучшению SEO, включая:
            1. Структуру текста
            2. Использование ключевых слов
            3. Длину описания
            4. Уникальные особенности
            """

            # Запрос к OpenAI
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Ты - эксперт по SEO и оптимизации текста."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3,
                max_tokens=1000
            )

            # Парсим ответ
            result = response.choices[0].message.content
            recommendations = []
            
            # Разбиваем ответ на рекомендации
            for line in result.split('\n'):
                if line.strip() and not line.startswith(('1.', '2.', '3.', '4.')):
                    recommendations.append({
                        'title': line.split(':')[0] if ':' in line else line,
                        'description': line.split(':')[1].strip() if ':' in line else line
                    })

            return recommendations

        except Exception as e:
            print(f"Ошибка при генерации рекомендаций: {str(e)}")
            return self._basic_seo_recommendations(description, keywords, competitors)

    def _basic_seo_recommendations(self, description, keywords, competitors):
        """Базовый метод генерации рекомендаций без использования AI"""
        recommendations = []
        
        # Анализ длины описания
        if len(description) < 500:
            recommendations.append({
                'title': 'Увеличьте длину описания',
                'description': 'Текущее описание слишком короткое. Рекомендуется добавить больше полезной информации о товаре.'
            })
        elif len(description) > 2000:
            recommendations.append({
                'title': 'Сократите описание',
                'description': 'Описание слишком длинное. Рекомендуется сделать его более лаконичным и структурированным.'
            })
        
        # Анализ ключевых слов
        important_keywords = [k for k in keywords if k['important']]
        if len(important_keywords) < 3:
            recommendations.append({
                'title': 'Добавьте важные ключевые слова',
                'description': 'В описании недостаточно важных ключевых слов. Рекомендуется добавить больше релевантных терминов.'
            })
        
        # Анализ структуры
        if not re.search(r'\n', description):
            recommendations.append({
                'title': 'Улучшите структуру',
                'description': 'Рекомендуется разбить описание на абзацы для лучшей читаемости.'
            })
        
        return recommendations

    def generate_optimized_description(self, current_description, keywords, competitors, recommendations):
        """Генерация оптимизированного описания с помощью OpenAI"""
        try:
            # Подготавливаем данные о ключевых словах
            keywords_info = "\n".join([
                f"{k['text']} (важность: {k['importance']})"
                for k in keywords if k['important']
            ])
            
            # Подготавливаем промпт
            prompt = f"""Перепиши описание товара с учетом SEO-оптимизации.
            
            Текущее описание:
            {current_description}
            
            Важные ключевые слова:
            {keywords_info}
            
            Рекомендации по улучшению:
            {json.dumps(recommendations, ensure_ascii=False, indent=2)}
            
            Создай новое описание, которое:
            1. Сохраняет всю важную информацию
            2. Использует ключевые слова естественным образом
            3. Имеет четкую структуру
            4. Оптимизировано для поисковых систем
            """

            # Запрос к OpenAI
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Ты - эксперт по SEO и копирайтингу."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=2000
            )

            # Получаем оптимизированное описание
            optimized = response.choices[0].message.content.strip()
            return optimized
            
        except Exception as e:
            print(f"Ошибка при генерации оптимизированного описания: {str(e)}")
            return self._basic_optimized_description(current_description, keywords, competitors, recommendations)

    def _basic_optimized_description(self, current_description, keywords, competitors, recommendations):
        """Базовый метод генерации оптимизированного описания без использования AI"""
        optimized = current_description
        
        # Добавляем важные ключевые слова, если их нет
        important_keywords = [k['text'] for k in keywords if k['important']]
        for keyword in important_keywords:
            if keyword not in optimized.lower():
                optimized += f"\n\n{keyword.capitalize()}"
        
        # Добавляем структуру, если её нет
        if not re.search(r'\n', optimized):
            optimized = optimized.replace('. ', '.\n')
        
        return optimized

def save_to_csv(products, filename):
    """Сохранение продуктов в CSV файл"""
    if not products:
        return None
    
    headers = ['№'] + list(products[0].keys())
    
    with open(filename, 'w', newline='', encoding='utf-8-sig') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(headers)
        
        for i, product in enumerate(products, 1):
            row = [i] + list(product.values())
            writer.writerow(row)
    
    return filename

def save_to_xlsx(products, filename):
    """Сохранение продуктов в XLSX файл"""
    if not products:
        return None
    try:
        # Создаем DataFrame
        df = pd.DataFrame(products)
        # Добавляем номер строки
        df.insert(0, '№', range(1, len(df) + 1))
        # Сохраняем в Excel
        with pd.ExcelWriter(filename, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Товары')
            # Получаем рабочий лист
            worksheet = writer.sheets['Товары']
            # Настраиваем ширину столбцов
            for idx, col in enumerate(df.columns):
                max_length = max(
                    df[col].astype(str).apply(len).max(),
                    len(str(col))
                )
                worksheet.column_dimensions[chr(65 + idx)].width = min(max_length + 2, 50)
        print(f"✓ XLSX файл успешно создан: {filename}")
        return filename
    except Exception as e:
        print(f"✗ Ошибка при создании XLSX файла: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

def save_to_pdf(products, filename):
    """Сохранение продуктов в PDF файл"""
    if not products:
        return None
    try:
        # Создаем PDF документ
        doc = SimpleDocTemplate(
            filename,
            pagesize=landscape(letter),
            rightMargin=30,
            leftMargin=30,
            topMargin=30,
            bottomMargin=30
        )
        # Подготавливаем данные для таблицы
        headers = ['№'] + list(products[0].keys())
        data = [headers]
        for i, product in enumerate(products, 1):
            row = [i] + list(product.values())
            data.append(row)
        # Создаем таблицу
        table = Table(data)
        # Стили для таблицы
        style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.white),
            ('TEXTCOLOR', (0, 1), (-1, -1), colors.black),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ])
        table.setStyle(style)
        # Добавляем таблицу в документ
        elements = []
        elements.append(table)
        # Строим PDF
        doc.build(elements)
        print(f"✓ PDF файл успешно создан: {filename}")
        return filename
    except Exception as e:
        print(f"✗ Ошибка при создании PDF файла: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

@app.route('/')
def index():
    return jsonify({"status": "ok", "message": "Wildberries Parser API is running"})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

@app.route('/parse', methods=['POST'])
def parse():
    try:
        data = request.get_json()
        seller_url = data.get('seller_url')
        file_format = data.get('format', 'csv')  # По умолчанию CSV
        print(f"Получен запрос на парсинг. URL: {seller_url}, Формат: {file_format}")  # Отладочный вывод
        if not seller_url:
            return jsonify({'error': 'URL продавца не указан'}), 400
        # Парсинг товаров
        parser = WildberriesParser()
        products = parser.parse_seller_products(seller_url)
        if not products:
            return jsonify({'error': 'Товары не найдены'}), 404
        # Генерация имени файла с учетом формата
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'products_{timestamp}.{file_format}'
        # Сохранение в выбранном формате
        if file_format == 'xlsx':
            save_to_xlsx(products, filename)
        elif file_format == 'pdf':
            save_to_pdf(products, filename)
        else:  # По умолчанию CSV
            save_to_csv(products, filename)
        # Получаем размер файла
        file_size = os.path.getsize(filename)
        file_size_mb = round(file_size / (1024 * 1024), 2)
        print(f"Файл сохранен: {filename}, размер: {file_size_mb} МБ")  # Отладочный вывод
        return jsonify({
            'success': True,
            'products_count': len(products),
            'filename': filename,
            'format': file_format,
            'file_size_mb': file_size_mb
        })
    except Exception as e:
        print(f"Ошибка при парсинге: {str(e)}")  # Отладочный вывод
        return jsonify({'error': f'Ошибка парсинга: {str(e)}'}), 500

@app.route('/check-position', methods=['POST'])
def check_position():
    """Endpoint для проверки позиций товара"""
    print("\n=== НОВЫЙ ЗАПРОС check-position ===")
    try:
        data = request.json
        product_url = data.get('product_url')
        keywords = data.get('keywords', [])
        
        print(f"Получены данные:")
        print(f"  URL: {product_url}")
        print(f"  Ключевые слова: {keywords}")
        
        if not product_url or not keywords:
            print("ОШИБКА: Не все параметры указаны")
            return jsonify({'error': 'Не все параметры указаны'}), 400
        
        parser = WildberriesParser()
        results = []
        
        for keyword in keywords[:10]:
            if keyword and keyword.strip():
                try:
                    print(f"\nПоиск для ключевого слова: '{keyword}'")
                    position = parser.search_product_position(product_url, keyword.strip())
                    
                    # Гарантируем, что position - это целое число >= 0
                    if position is None or not isinstance(position, (int, float)) or position < 0:
                        position = 0
                    else:
                        position = int(position)
                    
                    results.append({
                        'keyword': keyword.strip(),
                        'position': position
                    })
                    print(f"Результат: позиция {position}")
                    
                except Exception as e:
                    print(f"ОШИБКА при поиске для '{keyword}': {e}")
                    import traceback
                    traceback.print_exc()
                    results.append({
                        'keyword': keyword.strip(),
                        'position': 0
                    })
                
                time.sleep(0.5)
        
        print(f"\nИтоговые результаты: {results}")
        return jsonify({
            'success': True,
            'results': results
        })
        
    except Exception as e:
        print(f"\nКРИТИЧЕСКАЯ ОШИБКА в check_position: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/analyze-adrates', methods=['POST'])
def analyze_adrates():
    """Endpoint для анализа рекламных ставок"""
    try:
        data = request.json
        query = data.get('query')
        region = data.get('region', '-1114822')
        
        if not query:
            return jsonify({'error': 'Поисковый запрос не указан'}), 400
        
        parser = WildberriesParser()
        results = parser.analyze_ad_rates(query, region)
        
        if not results:
            return jsonify({'error': 'Не удалось получить данные о ставках. Попробуйте другой запрос.'}), 404
        
        return jsonify({
            'success': True,
            'results': results
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/analyze-competitors', methods=['POST'])
def analyze_competitors():
    """Endpoint для анализа конкурентов"""
    try:
        data = request.json
        product_url = data.get('product_url')
        
        if not product_url:
            return jsonify({'error': 'Ссылка на товар не указана'}), 400
        
        parser = WildberriesParser()
        results = parser.analyze_competitors(product_url)
        
        return jsonify(results)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/analyze-seo', methods=['POST'])
def analyze_seo():
    """Endpoint для анализа SEO"""
    print("\n=== НОВЫЙ ЗАПРОС analyze-seo ===")
    try:
        # Получаем сырые данные запроса
        raw_data = request.get_data()
        print(f"Сырые данные запроса: {raw_data}")
        
        # Получаем данные в формате JSON
        data = request.get_json()
        print(f"Данные в формате JSON: {data}")
        
        if not data:
            print("ОШИБКА: Данные не получены")
            return jsonify({'error': 'Данные не получены'}), 400
            
        # Получаем URL из данных
        product_url = data.get('product_url')
        print(f"URL из данных: {product_url}")
        
        if not product_url:
            print("ОШИБКА: URL не найден в данных")
            return jsonify({'error': 'URL не найден в данных'}), 400
            
        # Очищаем URL
        product_url = str(product_url).strip()
        if product_url.startswith('@'):
            product_url = product_url[1:].strip()
            
        print(f"URL после очистки: {product_url}")
        
        if not product_url:
            print("ОШИБКА: URL пустой после очистки")
            return jsonify({'error': 'URL пустой после очистки'}), 400
            
        # Проверяем, содержит ли URL ID товара
        match = re.search(r'/catalog/(\d+)/', product_url)
        if not match:
            print(f"ОШИБКА: Не удалось найти ID товара в URL: {product_url}")
            return jsonify({'error': 'Неверный формат ссылки на товар Wildberries'}), 400
            
        # Извлекаем ID товара и формируем правильный URL
        product_id = match.group(1)
        product_url = f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx"
        print(f"Финальный URL: {product_url}")
        
        parser = WildberriesParser()
        results = parser.analyze_seo(product_url)
        print(f"Результаты анализа: {json.dumps(results, ensure_ascii=False, indent=2)}")
        
        return jsonify(results)
        
    except Exception as e:
        print(f"ОШИБКА при анализе SEO: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/download/<filename>')
def download_file(filename):
    try:
        print(f"Запрос на скачивание файла: {filename}")  # Отладочный вывод
        return send_file(filename, as_attachment=True)
    except Exception as e:
        print(f"Ошибка при скачивании файла: {str(e)}")  # Отладочный вывод
        return jsonify({'error': 'Файл не найден'}), 404

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8000))
    app.run(host='0.0.0.0', port=port)