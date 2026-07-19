// Paste this entire file into Chrome DevTools Console while Shelves Finder is open.
(() => {
    const mixedUrl = 'https://www.walmart.com/browse/electronics/headphones/3944_96469';
    const organicUrl = 'https://www.walmart.com/browse/electronics/bluetooth-headphones/3944_96469_1231';
    const sponsoredUrl = 'https://www.walmart.com/browse/electronics/over-ear-headphones/3944_96469_4561';
    const discoverableUrl = 'https://www.walmart.com/browse/electronics/gaming-headsets/3944_96469_7788';
    const missingUrl = 'https://www.walmart.com/browse/electronics/noise-cancelling/3944_96469_9911';

    const report = {
        product_title: 'Acme Wireless Noise-Cancelling Headphones',
        product_brand: 'Acme',
        product_id: '123456789',
        product_image: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Crect width='160' height='160' rx='20' fill='%23dbe2ea'/%3E%3Cpath d='M43 86V72a37 37 0 0174 0v14' fill='none' stroke='%2364748b' stroke-width='10'/%3E%3Crect x='32' y='78' width='24' height='48' rx='10' fill='%2398a2b3'/%3E%3Crect x='104' y='78' width='24' height='48' rx='10' fill='%2398a2b3'/%3E%3C/svg%3E",
        product_price: '$79.99',
        keywords_used: [
            'wireless headphones',
            'bluetooth headphones',
            'over ear headphones',
            'gaming headset',
            'noise cancelling headphones',
        ],
        shelf_results: [
            {
                url: mixedUrl,
                keyword: 'wireless headphones',
                position: 1,
                product_found: true,
                found: true,
                brand_found: true,
                page_number: 1,
                visibility: true,
                discoverability: true,
                organic: true,
                sponsored: true,
                placement_rank: 2,
                placements: [
                    {
                        placement_index: 1,
                        placement_rank: 2,
                        visibility: true,
                        discoverability: false,
                        organic: false,
                        sponsored: true,
                        classification_source: 'structured',
                    },
                    {
                        placement_index: 2,
                        placement_rank: 11,
                        visibility: true,
                        discoverability: true,
                        organic: true,
                        sponsored: false,
                        classification_source: 'structured',
                    },
                ],
            },
            {
                url: organicUrl,
                keyword: 'bluetooth headphones',
                position: 2,
                product_found: true,
                found: true,
                brand_found: true,
                page_number: 1,
                visibility: true,
                discoverability: true,
                organic: true,
                sponsored: false,
                placement_rank: 6,
                placements: [
                    {
                        placement_index: 1,
                        placement_rank: 6,
                        visibility: true,
                        discoverability: true,
                        organic: true,
                        sponsored: false,
                        classification_source: 'structured',
                    },
                ],
            },
            {
                url: sponsoredUrl,
                keyword: 'over ear headphones',
                position: 3,
                product_found: false,
                found: false,
                brand_found: false,
                page_number: 0,
                visibility: true,
                discoverability: false,
                organic: false,
                sponsored: true,
                placement_rank: 4,
                placements: [
                    {
                        placement_index: 1,
                        placement_rank: 4,
                        visibility: true,
                        discoverability: false,
                        organic: false,
                        sponsored: true,
                        classification_source: 'structured',
                    },
                ],
            },
            {
                url: discoverableUrl,
                keyword: 'gaming headset',
                position: 4,
                product_found: true,
                found: true,
                brand_found: true,
                page_number: 1,
                visibility: false,
                discoverability: true,
                organic: false,
                sponsored: false,
                placement_rank: null,
                placements: [],
            },
            {
                url: missingUrl,
                keyword: 'noise cancelling headphones',
                position: 5,
                product_found: false,
                found: false,
                brand_found: false,
                page_number: 0,
                visibility: false,
                discoverability: false,
                organic: false,
                sponsored: false,
                placement_rank: null,
                placements: [],
            },
        ],
        shelf_stats: {
            score: 60,
            found: 3,
            missing: 2,
            total: 5,
            visible: 3,
            discoverable: 3,
            placements: 4,
            organic: 2,
            sponsored: 2,
            organic_pages: 2,
            sponsored_pages: 2,
        },
        openai_cost_usd: 0.0381,
    };

    if (typeof window.renderShelvesFinderMockReport !== 'function') {
        throw new Error('Mock renderer is unavailable. Refresh Shelves Finder after loading the latest app.js.');
    }

    window.renderShelvesFinderMockReport(report);
})();
